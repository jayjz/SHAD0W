# ADR 0002: Research/execution separation using ports/adapters

- **Status:** accepted
- **Context:** Broker SDKs and provider schemas would otherwise contaminate simulation and strategy research.
- **Decision:** Keep provider-neutral domain and research logic separate from data and execution adapters; introduce broker connectivity only behind ports when a milestone requires it.
- **Consequences:** P0–P3 remain broker-independent and testable from recorded inputs. Adapters incur translation work but cannot dictate cross-system contracts.
