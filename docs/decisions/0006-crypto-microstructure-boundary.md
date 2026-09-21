# ADR 0006: Crypto microstructure evidence boundary

- **Status:** accepted for C0; C1 runtime implementation remains planned.
- **Context:** Existing equity observations use microsecond datetimes, and P4A
  capture/composition imports strategy and risk contracts. Reusing them would lose
  crypto timing precision or introduce authority outside the research assignment.
- **Decision:** Add dedicated immutable provider-neutral trade, quote and book
  event values with exact integer UTC nanoseconds and distinct receipt availability.
  Reuse Instrument, Provenance and truthful system-receipt/classification semantics.
  Restrict C1 to BTC/USD and ETH/USD. Keep the future crypto adapter, reducer and
  `shadow.crypto-market.v1` evidence separate from equity/P5A paths.
- **Evidence:** Raw market events are the research source; provider bars are not
  authoritative primitives. Resets establish snapshots; updates yield only
  RECONSTRUCTED_UNVERIFIED state. Missing resets and detected discontinuities
  produce UNKNOWN/UNTRUSTED states. Local ordering cannot prove provider completeness.
- **Consequences:** Some duplicated value validation is preferable to broadening
  generic contracts or importing operational authority. C0 exposes no conversion,
  reducer, storage, network or measurement abstraction. Domain enum vocabulary
  does not implement quality transitions. Existing market/DAY risk and SPY PAPER
  behavior remain unchanged; no profitability or prediction claim is implied.
- **Contract:** [Crypto market-data contract](../CRYPTO_MARKET_DATA_CONTRACT.md)
  defines time, identity, validation, future evidence requirements and research limits.
