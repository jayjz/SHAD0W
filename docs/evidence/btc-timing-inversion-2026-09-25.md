# BTC timing inversion inspection — 2026-09-25

This is read-only transport evidence. It is not an order, a PAPER submission, or
permission to accept a provider timestamp that is later than local receipt.

## What was not run

No Alpaca market-data credentials were available in the inspection environment
(`ALPACA_DATA_KEY`, `ALPACA_DATA_SECRET`, and the API-key aliases were unset).
No relay was connected to Alpaca. No consumer socket was opened. No broker
adapter was constructed. No PAPER order was submitted and no broker POST
occurred.

The kernel clock on this runner, from read-only `adjtimex(modes=0)`, was
`STA_UNSYNC` with offset `0`, jitter `0`, and `maxerror`/`esterror` of 16
seconds. `timedatectl` was not installed. That snapshot describes this runner,
not the host that produced the earlier halt.

## Prior observation being classified

The 2026-09-25 minimum-sizing halt recorded four relay-forwarded BTC quotes
whose local receipt minus provider observation was about `-299.900`,
`-299.804`, `-299.750`, and `-299.655` ms. Those receipts were not yet split
into relay-receive and consumer-receive stamps. The supplied host snapshot was:

| Field | Value |
| --- | --- |
| NTP | yes |
| NTPSynchronized | yes |
| server | ntp.ubuntu.com |
| stratum | 2 |
| offset | -66.669 ms |
| delay | 98.075 ms |
| jitter | 346.799 ms |
| frequency | +142.470 ppm |

NTP/timesyncd offset is the correction added to the local clock (RFC 5905). A
negative offset means the local clock is ahead of the reference. A negative
receipt-minus-provider delta means provider `t` is ahead of local receipt.

## Classifier result on that supplied pair

The new diagnostic classifier was applied to the prior lead as if both the
relay and consumer wall receipts shared it, with a sub-millisecond monotonic
hop and exact nanosecond parsing. Using the supplied timesyncd numbers:

| Result | Value |
| --- | --- |
| cause | `insufficient_evidence` |
| confidence | low |
| inversion treated as reproduced | yes, from the prior halt only |
| trading authority | false |

The offset sign does not match a local-clock-behind explanation of a provider
lead, while jitter of about 347 ms is large enough that host-clock error is
also not excluded. Provider timestamp semantics are therefore not demonstrated
either. This is not a new live measurement.

## What the code inspection and tests do show

- Alpaca `t` is still parsed with integer nanosecond arithmetic, and a +300 ms
  suffix is exactly `300_000_000` ns.
- The relay samples wall and monotonic time only after the provider frame
  receive returns, and it forwards that frame unchanged.
- The consumer samples wall and monotonic time only after the downstream
  receive returns. Availability remains that wall sample.
- A negative relay-to-consumer monotonic delta is classified as a capture
  defect. The healthy in-process path keeps relay monotonic receipt at or
  before consumer monotonic receipt.
- A provider observation 300 ms after local receipt, and a provider observation
  1 ns after receipt, still raise `MarketDataValidationError`. Equal receipt
  remains legal. No tolerance was added.
- A stale provider timestamp is retained and labeled only. It is not rewritten.
- Timing JSONL is schema `shadow.crypto-timing.v1`, sets `trading_authority`
  to false, and omits credentials and auth frames.

## Later command

On the host that owns the Alpaca market-data keys and the clock under test:

```bash
uv run shadow-crypto-feed-relay \
  --timing-evidence /tmp/shadow-crypto-timing-relay.jsonl \
  --timing-max-events 16 \
  --code-revision "$(git rev-parse HEAD)"

uv run shadow-crypto-timing-observe \
  --relay ws://127.0.0.1:8766 \
  --evidence-path /tmp/shadow-crypto-timing-consumer.jsonl \
  --max-events 8 \
  --timeout-seconds 20 \
  --code-revision "$(git rev-parse HEAD)"

uv run shadow-crypto-timing-observe --join \
  --relay-evidence /tmp/shadow-crypto-timing-relay.jsonl \
  --consumer-evidence /tmp/shadow-crypto-timing-consumer.jsonl
```

Do not start `shadow-crypto-paper` or `shadow-btc-paper-session` for this
measurement. Do not pass `--trading-enabled`. If the joined report remains
`insufficient_evidence`, keep failing closed.
