"""Pure Alpaca PAPER client-order identity mapping; no transport authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

CLIENT_ID_MAPPING_VERSION = "shadow.alpaca.paper.client-order.v1"
_PREFIX = "shp1_"
_DIGEST_HEX_LENGTH = 40


class PaperClientIdentityError(ValueError):
    """An invalid or colliding deterministic client identity."""


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PaperClientIdentityError(f"{field} must be a nonempty trimmed string")


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class PaperClientOrderIdentity:
    """A reconciliation key, explicitly insufficient to authorize a retry or POST."""

    stable_account_binding: str
    operational_scope: str
    intent_identity: str
    mapping_version: str
    full_digest: str
    client_order_id: str

    def __post_init__(self) -> None:
        for field in ("stable_account_binding", "operational_scope", "intent_identity"):
            _text(getattr(self, field), field)
        if self.mapping_version != CLIENT_ID_MAPPING_VERSION:
            raise PaperClientIdentityError("unsupported client-ID mapping version")
        if len(self.full_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.full_digest
        ):
            raise PaperClientIdentityError("full_digest must be a lowercase SHA-256 digest")
        expected = _PREFIX + self.full_digest[:_DIGEST_HEX_LENGTH]
        if self.client_order_id != expected:
            raise PaperClientIdentityError("client_order_id does not match full digest")


def derive_paper_client_order_identity(
    *, stable_account_binding: str, operational_scope: str, intent_identity: str
) -> PaperClientOrderIdentity:
    """Derive the contract's deterministic ID.  It makes no retry decision."""
    for value, field in (
        (stable_account_binding, "stable_account_binding"),
        (operational_scope, "operational_scope"),
        (intent_identity, "intent_identity"),
    ):
        _text(value, field)
    encoded = json.dumps(
        [CLIENT_ID_MAPPING_VERSION, stable_account_binding, operational_scope, intent_identity],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    full_digest = _sha256_hex(encoded)
    return PaperClientOrderIdentity(
        stable_account_binding=stable_account_binding,
        operational_scope=operational_scope,
        intent_identity=intent_identity,
        mapping_version=CLIENT_ID_MAPPING_VERSION,
        full_digest=full_digest,
        client_order_id=_PREFIX + full_digest[:_DIGEST_HEX_LENGTH],
    )


class PaperClientIdentityRegistry:
    """Detect truncated-ID collisions before a later journal can persist one."""

    def __init__(self) -> None:
        self._by_client_order_id: dict[str, PaperClientOrderIdentity] = {}

    def register(self, identity: PaperClientOrderIdentity) -> PaperClientOrderIdentity:
        if not isinstance(identity, PaperClientOrderIdentity):
            raise PaperClientIdentityError("identity must be a PaperClientOrderIdentity")
        existing = self._by_client_order_id.get(identity.client_order_id)
        if existing is None:
            self._by_client_order_id[identity.client_order_id] = identity
            return identity
        if existing != identity:
            raise PaperClientIdentityError("truncated client-order ID collision")
        return existing
