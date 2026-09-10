# ADR 0003: LLM/event intelligence has observational authority only

- **Status:** accepted
- **Context:** Future unstructured news or filing processing may benefit from semantic classification but is nondeterministic and not a safety authority.
- **Decision:** Any LLM may only produce a typed, time-bounded `EventRiskState` for deterministic consumers. It cannot submit orders, size positions, set or override limits, change experiment parameters, bypass risk, or determine profitability.
- **Consequences:** Deterministic risk and evidence systems remain authoritative. LLM outputs require provenance and expiry if introduced later.
