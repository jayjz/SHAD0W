# Data contracts

P0.1 implements provider-neutral Python domain values in `shadow.domain` and deterministic validation/identity functions in `shadow.data`. They use only the Python standard library. Provider SDK, dataframe, HTTP, database, and LLM objects are not accepted as cross-system contracts; a future adapter must translate them before this boundary.

## Time and observation semantics

All accepted timestamps must be timezone-aware. Contract construction normalizes them to UTC; a naive timestamp is an explicit validation failure and is never assumed to be local time or UTC. `Provenance` and `DatasetMetadata` may retain source timezone and session descriptions without treating those strings as a trading calendar.

`Bar.observation_time` always means the **end of the represented interval**. It is neither an interval-start convention nor an unexamined provider timestamp. `availability_time` is a separate UTC instant: the earliest time a model may legally consume that record. Its `AvailabilitySemantics` is one of `provider_published`, `system_received`, or `modeled`. Availability cannot precede observation. This is a data contract only; P0.1 does not make decisions or simulate fills.

## Executable values and validation

`Instrument` is a non-empty trimmed provider-neutral identifier and `BarInterval` is a positive `timedelta`. `Provenance` identifies the observation source and may record source timezone/session context.

`Bar` has an instrument, explicit interval, observation and availability times, OHLC values, optional volume, availability semantics, and provenance. Prices must be finite `Decimal` values. Volume is optional because not every source supplies it; when present it must be finite and non-negative, including zero. Bars allow values such as negative prices where a market can legitimately produce them, but enforce `high >= open`, `high >= close`, `low <= open`, `low <= close`, and `high >= low`.

`Quote` has finite `Decimal` bid/ask prices, optional non-negative finite bid/ask sizes, time/availability fields, and provenance. Locked and crossed quotes are retained rather than repaired or rejected; `market_state` classifies them as normal, locked, or crossed. This preserves source evidence without claiming every quote is executable.

`DatasetMetadata` provides required source, dataset identifier, instrument scope, UTC coverage start/end, schema version, and optional interval, retrieval method, timezone/session, adjustment policy, declared validation status, and quality notes. Bar datasets must declare one matching interval; quote datasets must not. Records must be chronological by nondecreasing observation time, scoped to the declared instruments/source/coverage, and have unique observation identities. A bar identity is instrument, interval, and observation time; a quote identity is instrument and observation time. Validation never sorts malformed input, deduplicates, fills, interpolates, or repairs it. It does not infer expected observations: without an explicit market calendar, an absent interval is an unknown expectation rather than automatically invalid missing data.

Failures use `MarketDataValidationError` with an invariant name and, for collection checks, a record index.

## Numeric policy

P0.1 accepts `Decimal` at the domain boundary. This avoids binary-float representation differences while remaining standard-library-only and sufficient for deterministic research-data canonicalization. Adapter code must deliberately convert external numeric values. Decimal NaN and infinities are rejected. Canonical serialization renders all numerical zero, including `Decimal("-0")`, as `"0"`; equal finite Decimal values with different exponent/trailing-zero representation serialize identically. Integer fixed-point was not chosen because asset/source precision varies and no P0.1 scale contract exists.

## Canonical dataset identity

`canonical_dataset_bytes(observations, metadata)` first validates a non-empty homogeneous bar or quote sequence, then emits canonical UTF-8 JSON. Fields are written in documented fixed order, timestamps use UTC ISO-8601 with six fractional digits and `Z`, Decimal values use normalized fixed-point strings, and interval duration uses exact integer microseconds. Input must be chronological; equal-time valid records are sorted by their complete canonical record bytes so their incidental input order does not change identity. Duplicate observations are rejected before serialization.

`dataset_fingerprint` returns the lowercase hexadecimal SHA-256 digest of those bytes. The canonical metadata includes schema version, observation kind, source, dataset ID, retrieval method, sorted instrument identifiers, interval, coverage, source timezone/session, and adjustment policy. `quality_notes` and declared `validation_status` are deliberately excluded because they describe a delivery or its commentary, not the canonical observations or their interpretation; validation itself is always rerun before fingerprinting. The digest identifies these exact canonical bytes. It is not a claim about provenance truth, provider authenticity, or completeness.

The repository-owned JSON fixtures are synthetic, require no credentials or network access, and exercise valid bars/quotes plus out-of-order, duplicate, malformed-OHLC, and naive-timestamp failures. Their expected valid-bar digest is pinned in tests.
