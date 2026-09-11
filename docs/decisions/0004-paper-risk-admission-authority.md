# ADR 0004: Paper dispatch requires a gate-issued one-use grant

- **Status:** accepted
- **Context:** A pure authorized risk decision is reproducible evidence but cannot prevent two consumers from submitting the same proposal. Treating that evidence as submission capability would also let reconstructed history bypass current admission state.
- **Decision:** P2A separates pure `RiskDecision` evidence from a gate-issued `AuthorizedOrder`. One process-local `RiskGate` per declared scope atomically records the first decision and reservation before issuing a grant. The supported dispatch seam consumes a genuine recorded grant once.
- **Consequences:** Raw signals, intents, lifecycle/economic evidence, and risk decisions cannot dispatch through the supported API. Duplicate delivery cannot produce a second grant, and reservations remain until future authoritative reconciliation. The guarantee is process-local and neither cryptographic nor restart-safe; P5A must add durable broker reconciliation before external paper submission.

## Adversarial closure

The first decision remains terminal even when missing/stale evidence or an automation freeze caused rejection. This intentionally sacrifices recovery of that source opportunity; it does not assert execution. The policy is fixed for the gate lifetime. There is no supported policy replacement or safe restart-based retry.

The one-use claim proves **admission-time authorization only**. It has no expiry and does not revalidate controls, policy, or evidence freshness; later control changes do not revoke a grant. It is insufficient for external submission. Before a broker adapter may dispatch, P5A must establish submission-time controls/policy/freshness, durable idempotency, and authoritative reservation reconciliation. P2A adds none of that infrastructure.

Risk model v2 rejects self-contradictory signal-rule evidence and only deduplicates fully equal reservations. A colliding reference cannot hide a gate reservation. Reservations never release after claim, abandonment, or a reported fill, so P2A cannot operate a continuous trading lifecycle. The kill switch freezes new entry/exit admissions; it neither revokes existing claims nor liquidates positions.
