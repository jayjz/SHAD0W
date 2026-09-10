# ADR 0001: Deterministic core

- **Status:** accepted
- **Context:** Research findings are only useful when another run can reconstruct the decisions that produced them.
- **Decision:** Core research behavior will be deterministic for identical data, configuration, implementation, and seed. Any explicit nondeterminism must be recorded in evidence.
- **Consequences:** Time and availability semantics, configuration identity, and seeds become first-class test and evidence concerns. External integrations must not define core behavior.
