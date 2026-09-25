# Shared Alpaca crypto feed relay

The Alpaca crypto market-data connection limit can reject a second local
WebSocket with error `406`. `tools/alpaca_crypto_feed_relay.py` prevents that
collision by making one localhost process the sole owner of:

`wss://stream.data.alpaca.markets/v1beta3/crypto/us`

```text
Alpaca crypto WebSocket
          |
          v
localhost crypto relay :8766
       /                 \
SHAD0W BTC PAPER     LIGHTLIGHT BTC worker
```

The relay is market-data-only. It binds only `127.0.0.1`, authenticates with
`ALPACA_DATA_KEY`/`ALPACA_DATA_SECRET` (or the existing API-key aliases), and
always requests exactly BTC/USD `bars`. It accepts no upstream URL, credentials,
wildcard, or arbitrary symbol from clients. To preserve the current SHAD0W BTC
PAPER application's trade-built interval semantics, it may additionally request
only fixed BTC/USD `trades` and `quotes` after that local client asks for them.
This is the smallest necessary extension beyond a bars-only relay; no client can
select another instrument or channel.

Provider bar OHLCV and timestamp values are not normalized, fabricated, or
rewritten by the relay. A bar-only provider frame is forwarded byte-for-byte.
Historical REST bootstrap/recovery is independent of this fanout.

## Local protocol and failure behavior

A LIGHTLIGHT-compatible bar client sends:

```json
{"action":"subscribe","bars":["BTC/USD"]}
```

The current SHAD0W BTC PAPER client sends its fixed existing input channels:

```json
{"action":"subscribe","trades":["BTC/USD"],"quotes":["BTC/USD"]}
```

The relay acknowledges the exact accepted local subset with a provider-shaped
`subscription` frame. It forwards only `b`, `t`, and `q` events for BTC/USD to
the respective local subscribers. `/health` returns a sanitized `relay_health`
object with `connecting`, `authenticated`, `subscribed`, `reconnecting`,
`failed`, or stopped state; it never includes credentials.

Alpaca may include an empty `orderbooks` field in its upstream subscription
acknowledgement even though the relay never requests that channel. The relay
accepts that field only when it is empty; a non-empty or otherwise unsupported
acknowledgement fails closed.

The provider can emit an otherwise valid fixed BTC/USD event before a changed
subscription acknowledgement. The relay drops only such pre-ack events, so a
local subscriber always receives its acknowledgement before provider data;
anything other than an expected BTC/USD `b`, `t`, or `q` event still fails
closed.

Alpaca subscriptions are additive. Once the relay adds fixed BTC/USD trades or
quotes for a local SHAD0W client, it retains those fixed upstream channels until
the one upstream socket reconnects; fanout still forwards them only to current
local subscribers. It never sends an arbitrary unsubscribe or another symbol.

Authentication, malformed upstream frames, subscription rejection, and local
protocol errors fail closed. A provider `406` becomes the explicit
`upstream_connection_limit` reconnecting state and uses bounded exponential
backoff; the relay closes the old upstream socket before it attempts another.
Transport loss closes downstream clients with code `1011`; a slow local client
alone closes with `1013`. No bar is synthesized during a reconnect and relay
mode never makes a consumer fall back to a direct provider socket.

## Operator workflow

Start the relay before consumers:

```bash
uv run shadow-crypto-feed-relay
```

Read its sanitized control state:

```bash
uv run shadow-crypto-feed-relay --health
```

Use the current SHAD0W BTC PAPER CLI's explicit relay flag in an otherwise
complete `--experiment` invocation:

```bash
uv run shadow-crypto-paper --experiment --market-data-relay ws://127.0.0.1:8766 \
  --account-id ACCOUNT --operational-scope SCOPE --quantity QTY \
  --journal-path JOURNAL --market-evidence-path MARKET_EVIDENCE \
  --experiment-evidence-path EXPERIMENT_EVIDENCE --ownership-directory OWNERSHIP \
  --duration-seconds SECONDS --paper-acknowledgement I-UNDERSTAND-THIS-SUBMITS-ONE-BTC-ALPACA-PAPER-ORDER \
  --run-id RUN --source-market-id MARKET --code-revision REVISION \
  --maximum-entry-notional NOTIONAL --cash-buffer BUFFER --trading-enabled
```

That command retains its existing PAPER guardrails; the relay flag changes only
the live market-data transport. It still uses direct read-only historical REST
for warm start. Do not use the relay flag with a different URL: only
`ws://127.0.0.1:8766` and `ws://localhost:8766` are accepted.

After the separate LIGHTLIGHT handoff in
[LIGHTLIGHT_CRYPTO_RELAY_HANDOFF.md](LIGHTLIGHT_CRYPTO_RELAY_HANDOFF.md) is
applied, start its durable worker with:

```bash
ALPACA_CRYPTO_LOCAL_FEED_URL=ws://127.0.0.1:8766 npm run btc:worker -- start
```

Run the read-only observer only briefly for inspection; it is not a worker:

```bash
npm run btc:observe
```

Inspect relay-port ownership locally:

```bash
ss -ltnp 'sport = :8766'
```

The relay's health response must show one process owning the upstream. Verify
from the Alpaca-side connection dashboard/logs that the same credential pair has
one crypto WebSocket, not one per consumer. Stop consumers first, then send
`SIGTERM`/`Ctrl-C` to the relay; restart the relay before restarting consumers.

For an eventual always-on host, a systemd **user** service may run
`uv run shadow-crypto-feed-relay` with only the relay's market-data credentials,
and a separate service may run LIGHTLIGHT's `npm run btc:worker -- start` with
`ALPACA_CRYPTO_LOCAL_FEED_URL`. Set `After=`/`Requires=` between the two units
and configure restart backoff. Do not enable or start either user service until
the host operator has reviewed its environment, ownership, and logs.

## Read-only timing evidence

Provider frames forwarded on `/` stay unchanged. Optional timing evidence is a
separate JSONL file, schema `shadow.crypto-timing.v1`, and it has no trading
authority. The relay samples wall and monotonic time only after the provider
websocket receive returns. The BTC consumer samples those clocks only after its
downstream receive returns. Those stamps are not `observation_time` or
`availability_time`.

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

The observer only subscribes to fixed BTC/USD trades and quotes. It does not
read broker credentials, submit an order, or enable trading. Stop it before
stopping the relay. Delete the JSONL files when the inspection is done; do not
feed them to the PAPER path.

`observation_time <= availability_time` stays exact. A host whose NTP service is
inactive, whose kernel clock is unsynchronized, or whose timesync offset/jitter
is large relative to the provider lead cannot supply `SYSTEM_RECEIVED`
availability. Fix the host clock. Do not loosen SHAD0W.
