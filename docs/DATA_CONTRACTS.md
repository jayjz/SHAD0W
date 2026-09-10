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

## P0.2A feature contracts

`shadow.features` accepts `Sequence[Bar]` plus `DatasetMetadata` and calls P0.1 `validate_bars` itself. Consequently a feature function does not accept quotes, dataframes, provider objects, malformed time order, duplicate bars, or mismatched dataset scope; it does not normalize them. Its output is a tuple of immutable `FeatureSnapshot` values aligned to the supplied bars, with the input sequence order retained.

Each snapshot identifies the provider-neutral `Instrument`, feature name, `close` input, `shadow.features.v1` implementation version, declared source dataset ID, observation time, availability time, and input-count window. The source dataset ID is retained as upstream lineage. P0.1's fingerprint of an entire delivery is intentionally not copied into every snapshot because appending later observations would then change an otherwise identical historical snapshot's lineage field.

Snapshot state makes lack of a number semantic: `ready` requires a finite `Decimal`; `warming_up` requires the reason `insufficient_history`; and `unavailable` has a named reason. There are no fabricated zero warm-up values. An invalid requested window or mathematical domain raises `FeatureComputationError`, so failure is distinct from non-readiness. P0.2A z-scores use `unavailable/zero_variance` when their denominator is zero.

All P0.2A primitives use bar `close`. The return primitive is one-period **simple return**, `(P_t / P_(t-1)) - 1`; its first same-instrument observation warms up and a zero prior close fails explicitly. Log returns were not added because the first mean-reversion feature kernel needs only the simpler, negative-price-compatible close-to-close primitive and logarithms would impose a positive-price domain. Rolling mean, variance, standard deviation, and z-score use strict full trailing windows. Variance is **population variance** (`ddof=0`), and z-score is `(current_close - rolling_mean) / population_standard_deviation` over that same full window.

Feature arithmetic stays in `Decimal`, using a new local context for every calculation (34 significant digits, `ROUND_HALF_EVEN`) rather than the mutable process-wide Decimal context. Finite result values are canonically normalized, including zero to `Decimal("0")`. This is the P0.2A numerical boundary: no float, NumPy, Pandas, or runtime numerical dependency is used.

For every ready or explicitly unavailable snapshot, its availability is `max(availability_time)` of the same-instrument input bars used for its trailing window; a warm-up snapshot uses the partial trailing history that establishes that it is not ready. Thus feature availability can lag the aligned bar's observation time, including when an earlier bar was published later. This is an availability rule, not an observation-order shortcut.

## P0.3 strategy signal contracts

`shadow.strategies` consumes P0.2A snapshots rather than market-provider values. The sole current strategy accepts only a matching ready close z-score at its configured rolling window and implementation version. At an explicit timezone-aware decision time, evidence is rejected if absent, not ready, for another instrument/feature/version/window, not yet available, or older than its configured freshness allowance. It emits either no signal, a `long_entry` proposal while supplied context is `flat` and `z_score <= entry_threshold`, or an `exit` proposal while context is `holding` and `z_score >= exit_threshold`.

Signals are immutable evidence, not orders: they retain the provider-neutral instrument, strategy and configuration identities, material thresholds/freshness allowance, decision and availability times, feature identity/window/version/value/observation/availability time, reason, and source dataset ID. They contain no size, price, broker, fill, risk authorization, or portfolio state. This is an unvalidated hypothesis contract, not evidence of profitability.

## P0.4A chronological simulation contracts

`shadow.simulation` consumes immutable upstream evidence rather than recalculating features or strategy rules. It models four narrowly scoped event kinds: `market_observation_available`, `feature_available`, `signal_available`, and `execution_opportunity`. Every event carries a UTC-normalized effective time, provider-neutral instrument, caller-supplied stable event ID, and source reference. A signal event additionally carries the immutable P0.3 `Signal` and must occur exactly at its signal availability time. A modeled execution opportunity contains no price, quantity, order type, or fill assertion.

`observation_time` remains the end of a represented market interval and is never substituted for availability. An upstream market or feature event has effective time equal to its availability time. `decision_time` is the explicit time at which P0.3 evaluated legal feature evidence; the P0.3 signal contract makes its `availability_time` equal to this decision time. An execution opportunity's `event_time` is its own modeled instant. These are distinct concepts even where a fixture gives them the same timestamp.

The canonical event key is `(effective_time, causal precedence, instrument identifier, event ID)`, ascending. Equal-time causal precedence is explicitly market observation, feature, signal, execution opportunity; the final two fields make ordering independent of input/provider/object/hash order. A signal becomes a pending action whose exclusive eligibility boundary is its availability time. It is eligible only against the **first same-instrument execution opportunity with `event_time > signal_availability_time`**. Equal timestamps are explicitly ineligible, so a completed-bar signal cannot use the close that produced it. `TimelineRecord` gives structured evidence of each event or eligibility decision, including references/times for the signal and candidate opportunity and a machine-readable reason. No P0.4A record is a fill or trade.
