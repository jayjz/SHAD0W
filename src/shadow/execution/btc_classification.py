"""Independent proof and experiment readiness classifications for BTC PAPER.

Experimental readiness is deliberately limited to the initial flat entry seam.
It never manufactures fee-adjusted inventory from gross fills or broker position.
"""

from dataclasses import dataclass
from enum import StrEnum

from shadow.domain.market import Instrument
from shadow.execution.broker import BrokerSnapshot
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount
from shadow.execution.crypto_accounting import CryptoActivityEvidence, inventory_effects
from shadow.execution.reconciliation import OperationalState, Reconciliation, reconcile


class AuthorityClassification(StrEnum):
    PROOF_READY = "proof_ready"
    EXPERIMENT_READY = "experiment_ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class BtcAuthorityStatus:
    classification: AuthorityClassification
    proof_status: AuthorityClassification
    proof_blocker: str | None
    experiment_blockers: tuple[str, ...]
    observed_position: str | None
    lifecycle_state: OperationalState

    def __post_init__(self) -> None:
        if self.proof_status is AuthorityClassification.EXPERIMENT_READY:
            raise ValueError("proof status cannot be experimental")
        if tuple(sorted(set(self.experiment_blockers))) != self.experiment_blockers:
            raise ValueError("experiment blockers must be sorted and unique")
        if self.classification is AuthorityClassification.PROOF_READY and (
            self.proof_status is not AuthorityClassification.PROOF_READY or self.experiment_blockers
        ):
            raise ValueError("proof readiness requires strict proof readiness")
        if self.classification is AuthorityClassification.EXPERIMENT_READY and (
            self.proof_status is not AuthorityClassification.BLOCKED
            or self.proof_blocker != "provider_fee_linkage_finality"
            or self.experiment_blockers
        ):
            raise ValueError("experiment readiness requires the sole fee finality blocker")


def classify_btc_authority(
    *,
    account: BtcCashAccount,
    asset: BtcBrokerAsset,
    snapshot: BrokerSnapshot,
    activities: CryptoActivityEvidence,
    strict_lifecycle: Reconciliation,
    account_binding: tuple[str, str],
    journal_usable: bool,
    risk_fresh: bool,
    unresolved_prior_intent: bool,
) -> BtcAuthorityStatus:
    """Classify a complete broker cut without relaxing the proof reducer.

    EXPERIMENT_READY applies only before the first order, with a verified empty
    broker state and exhaustive, non-contradictory activity rows. The only
    tolerated gap is that fee linkage/finality cannot be proven. Once an attempt
    exists, the experiment must stop and use proof-grade reconciliation.
    """
    binding = account_binding
    blockers: set[str] = set()
    records = (account, asset, snapshot, activities)
    if any((row.evidence.account_id, row.evidence.operational_scope) != binding for row in records):
        blockers.add("account_binding")
    if asset.instrument != Instrument("BTC/USD") or not asset.tradable.value == "eligible":
        blockers.add("btc_asset_not_tradable")
    if not snapshot.complete or not snapshot.positions_complete or not snapshot.orders_complete:
        blockers.add("broker_history_incomplete")
    # `history_verified` is the strict provider coverage/finality contract.
    # Experimental entry tolerates only that documented gap: the query still
    # must be exhausted and it must contain no activity that could be unlinked.
    if not activities.query_exhausted:
        blockers.add("activity_query_incomplete")
    if activities.unsupported:
        blockers.add("unsupported_activity")
    if activities.executions or activities.fees:
        blockers.add("prior_or_unlinked_activity")
    if snapshot.orders or snapshot.positions:
        blockers.add("broker_state_not_initial_flat")
    if strict_lifecycle.state not in (OperationalState.FLAT, OperationalState.UNRESOLVED):
        blockers.add("strict_lifecycle_not_flat")
    if unresolved_prior_intent:
        blockers.add("unresolved_prior_intent")
    if not journal_usable:
        blockers.add("journal_unusable")
    if not risk_fresh:
        blockers.add("risk_stale_or_unauthorized")

    proof = AuthorityClassification.BLOCKED
    proof_blocker: str | None = None
    if strict_lifecycle.state is OperationalState.FLAT:
        proof = AuthorityClassification.PROOF_READY
    else:
        if not activities.history_verified and activities.query_exhausted:
            proof_blocker = "provider_fee_linkage_finality"
        else:
            try:
                inventory_effects(activities)
            except LookupError:
                proof_blocker = "provider_fee_linkage_finality"
            except ValueError:
                proof_blocker = None

    if proof is AuthorityClassification.PROOF_READY and not blockers:
        return BtcAuthorityStatus(
            AuthorityClassification.PROOF_READY,
            proof,
            None,
            (),
            None,
            strict_lifecycle.state,
        )
    # The experimental seam is only valid for a pristine empty cut. Don't
    # classify a post-fill or nonempty position by comparing gross quantity.
    if proof_blocker == "provider_fee_linkage_finality" and not blockers:
        return BtcAuthorityStatus(
            AuthorityClassification.EXPERIMENT_READY,
            AuthorityClassification.BLOCKED,
            proof_blocker,
            (),
            None,
            strict_lifecycle.state,
        )
    return BtcAuthorityStatus(
        AuthorityClassification.BLOCKED,
        proof,
        proof_blocker,
        tuple(sorted(blockers or {"strict_proof_reconciliation_unavailable"})),
        str(snapshot.positions[0].quantity) if len(snapshot.positions) == 1 else None,
        strict_lifecycle.state,
    )


def proof_reconcile(
    *, attempts: tuple[object, ...], snapshot: BrokerSnapshot, activities: CryptoActivityEvidence
) -> Reconciliation:
    """Named strict path for callers that need an explicit proof-grade cut."""
    return reconcile(
        attempts=attempts,  # type: ignore[arg-type]
        snapshot=snapshot,
        crypto_evidence=activities,
        require_crypto_evidence=True,
    )
