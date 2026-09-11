# ADR 0004: Paper dispatch requires a gate-issued one-use grant

- **Status:** accepted
- **Context:** A pure authorized risk decision is reproducible evidence but cannot prevent two consumers from submitting the same proposal. Treating that evidence as submission capability would also let reconstructed history bypass current admission state.
- **Decision:** P2A separates pure `RiskDecision` evidence from a gate-issued `AuthorizedOrder`. One process-local `RiskGate` per declared scope atomically records the first decision and reservation before issuing a grant. The supported dispatch seam consumes a genuine recorded grant once.
- **Consequences:** Raw signals, intents, lifecycle/economic evidence, and risk decisions cannot dispatch through the supported API. Duplicate delivery cannot produce a second grant, and reservations remain until future authoritative reconciliation. The guarantee is process-local and neither cryptographic nor restart-safe; P5A must add durable broker reconciliation before external paper submission.
