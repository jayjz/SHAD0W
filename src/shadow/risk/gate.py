"""Single-process admission and one-use dispatch authority for P2A paper orders."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock

from shadow.domain import Quote
from shadow.features import FeatureSnapshot

from .evaluator import evaluate_risk
from .models import (
    AuthorizedOrder,
    OrderIntent,
    OutstandingOrder,
    RiskContractError,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
    RiskRejectionReason,
    RiskState,
    _issue_authorized_order,
)


class DispatchAuthorizationError(RiskContractError):
    """A caller attempted to cross the dispatch seam without a live gate grant."""


@dataclass(frozen=True, slots=True)
class RiskAdmission:
    decision: RiskDecision
    authorization: AuthorizedOrder | None

    def __post_init__(self) -> None:
        if self.decision.status is RiskDecisionStatus.AUTHORIZED:
            if self.authorization is None:
                raise RiskContractError("authorized admission requires an authorization artifact")
        elif self.authorization is not None:
            raise RiskContractError("rejected admission cannot expose an authorization artifact")


@dataclass(slots=True)
class _AdmissionRecord:
    intent: OrderIntent
    decision: RiskDecision
    authorization: AuthorizedOrder | None
    gate_token: object | None
    dispatched: bool = False


class RiskGate:
    """Atomically decide, reserve, issue, and consume admission-time capability.

    One gate owner per operational scope is an explicit P2A assumption. State is
    process-local and is neither persistent nor evidence of broker state after restart.
    Policy is fixed for this gate's lifetime. Claims do not revalidate time or controls
    and are insufficient for external submission.
    """

    def __init__(self, *, operational_scope: str, policy: RiskPolicy) -> None:
        if not operational_scope or operational_scope != operational_scope.strip():
            raise RiskContractError("operational_scope must be a non-empty trimmed string")
        if not isinstance(policy, RiskPolicy):
            raise RiskContractError("policy must be a RiskPolicy")
        self._operational_scope = operational_scope
        self._policy = policy
        self._lock = Lock()
        self._history: dict[str, _AdmissionRecord] = {}
        self._reservations: dict[str, OutstandingOrder] = {}
        self._grants: dict[str, _AdmissionRecord] = {}

    @property
    def operational_scope(self) -> str:
        return self._operational_scope

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    @property
    def reservations(self) -> tuple[OutstandingOrder, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._reservations.values(),
                    key=lambda item: (item.instrument.identifier, item.intent_identity),
                )
            )

    @property
    def recorded_decisions(self) -> tuple[RiskDecision, ...]:
        with self._lock:
            return tuple(record.decision for record in self._history.values())

    def _effective_state(self, state: RiskState) -> RiskState:
        # Only a complete match represents the same reservation. Retain conflicts
        # so the evaluator can reject duplicate references/identities or instruments.
        existing_orders = set(state.outstanding_orders)
        gate_orders = tuple(
            order for order in self._reservations.values() if order not in existing_orders
        )
        if not gate_orders:
            return state
        return replace(
            state,
            state_id=f"{state.state_id}/gate-reservations",
            revision=state.revision + len(gate_orders),
            outstanding_orders=(*state.outstanding_orders, *gate_orders),
        )

    @staticmethod
    def _retry_decision(
        *,
        prior: RiskDecision,
        intent: OrderIntent,
        reason: RiskRejectionReason,
        decision_time: datetime,
    ) -> RiskDecision:
        return RiskDecision(
            intent=intent,
            status=RiskDecisionStatus.REJECTED,
            reasons=(reason,),
            policy_id=prior.policy_id,
            policy_fingerprint=prior.policy_fingerprint,
            risk_state_id=prior.risk_state_id,
            risk_state_revision=prior.risk_state_revision,
            risk_state_fingerprint=prior.risk_state_fingerprint,
            feature_reference=prior.feature_reference,
            quote_reference=prior.quote_reference,
            decision_time=decision_time,
            prior_decision_reference=prior.decision_id,
        )

    def admit(
        self,
        *,
        intent: OrderIntent,
        state: RiskState,
        feature: FeatureSnapshot,
        quote: Quote,
        decision_time: datetime,
    ) -> RiskAdmission:
        if not isinstance(intent, OrderIntent):
            raise RiskContractError("intent must be an OrderIntent")
        if intent.operational_scope != self._operational_scope:
            raise RiskContractError("intent operational_scope does not belong to this gate")
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise RiskContractError("decision_time must be timezone-aware")
        now = decision_time.astimezone(UTC)

        with self._lock:
            prior = self._history.get(intent.intent_identity)
            if prior is not None:
                reason = (
                    RiskRejectionReason.DUPLICATE_INTENT
                    if prior.intent.payload_fingerprint == intent.payload_fingerprint
                    else RiskRejectionReason.INTENT_IDENTITY_CONFLICT
                )
                return RiskAdmission(
                    decision=self._retry_decision(
                        prior=prior.decision,
                        intent=intent,
                        reason=reason,
                        decision_time=now,
                    ),
                    authorization=None,
                )

            effective_state = self._effective_state(state)
            decision = evaluate_risk(
                intent=intent,
                policy=self._policy,
                state=effective_state,
                feature=feature,
                quote=quote,
                decision_time=now,
            )
            record = _AdmissionRecord(
                intent=intent,
                decision=decision,
                authorization=None,
                gate_token=None,
            )
            self._history[intent.intent_identity] = record
            if decision.status is RiskDecisionStatus.REJECTED:
                return RiskAdmission(decision=decision, authorization=None)

            grant_id = hashlib.sha256(
                f"shadow.paper-grant.v1:{decision.decision_id}".encode()
            ).hexdigest()
            reservation = OutstandingOrder(
                instrument=intent.instrument,
                intent_identity=intent.intent_identity,
                side=intent.side,
                quantity=intent.quantity,
                reference=grant_id,
            )
            gate_token = object()
            self._reservations[intent.intent_identity] = reservation
            record.gate_token = gate_token
            self._grants[grant_id] = record
            authorization = _issue_authorized_order(
                grant_id=grant_id,
                intent=intent,
                decision=decision,
                gate_token=gate_token,
            )
            record.authorization = authorization
            return RiskAdmission(decision=decision, authorization=authorization)

    def claim_for_dispatch(self, authorization: object) -> OrderIntent:
        """Consume one recorded admission grant; this performs no external dispatch.

        This checks gate ownership and one-use consumption only. It has no expiry,
        current policy/control check, or freshness guarantee at claim time. Future
        external submission requires a separately designed revalidation boundary.
        Consumption does not release the reservation, even if the intent is abandoned.
        """
        if not isinstance(authorization, AuthorizedOrder):
            raise DispatchAuthorizationError("dispatch requires a gate-issued AuthorizedOrder")
        with self._lock:
            record = self._grants.get(authorization.grant_id)
            if (
                record is None
                or record.authorization != authorization
                or record.gate_token is not authorization._gate_token
                or record.decision.decision_id != authorization.decision_id
                or record.intent.intent_identity != authorization.intent_identity
                or record.intent.payload_fingerprint != authorization.payload_fingerprint
                or authorization.operational_scope != self._operational_scope
            ):
                raise DispatchAuthorizationError("authorization was not issued by this gate")
            if record.dispatched:
                raise DispatchAuthorizationError("authorization has already been consumed")
            record.dispatched = True
            return record.intent
