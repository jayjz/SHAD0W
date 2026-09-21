# Local Alpaca IEX feed relay

`tools/alpaca_feed_relay.py` is a separate, localhost-only market-data process.
It is deliberately not a broker adapter: it has no account, position, order,
execution, or PAPER-trade-update surface.

The relay is the sole process which authenticates to
`wss://stream.data.alpaca.markets/v2/iex`. It binds only to `ws://127.0.0.1:8765`.
No downstream message contains credentials.

The relay reads `ALPACA_DATA_KEY` / `ALPACA_DATA_SECRET`, or (for an existing
local operational environment) `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`.
Those variables belong only to the relay process; they are never sent to a
consumer or included in health output.

## Protocol

The downstream data endpoint is `ws://127.0.0.1:8765/`. A consumer must first
send exactly one JSON text frame:

```json
{"action":"subscribe","bars":["SPY"],"quotes":["SPY"]}
```

At least one channel is required; symbols must be unique uppercase ASCII
alphanumeric identifiers. The relay aggregates consumer requests and subscribes
the resulting channels upstream. Reconciliation on a connected upstream socket
is additive: removal of a local consumer does not send an upstream unsubscribe,
so unused symbols/channels can remain subscribed until the next upstream
reconnect. This is intentional and acceptable for today's SPY-only canary; it
is not exact dynamic subscription reconciliation. After Alpaca acknowledges a
requested subscription, the relay replies to the consumer with the
provider-shaped frame:

```json
[{"T":"subscription","bars":["SPY"],"quotes":["SPY"]}]
```

Subsequent frames are deterministic compact JSON arrays of the selected
original Alpaca `b` and `q` event objects. Event object fields, provider
timestamps (`t`), symbols (`S`), event types (`T`), and receive ordering are
preserved. The relay never synthesizes, normalizes, or replays data events.
It may split an upstream multi-event frame per local subscription; this changes
only transport framing, not event objects or their order.

`ws://127.0.0.1:8765/health` returns one `relay_health` JSON object and closes.
It contains only sanitized state, reconnect attempt count, local consumer count,
and last provider-frame receipt time.

Run `uv run python tools/alpaca_feed_relay.py --health` from SHAD0W to print it.

Each consumer has a queue limited to 32 frames. A full queue closes only that
consumer with WebSocket code `1013` and reason `slow_consumer_queue_full`.
An upstream disconnect closes local consumers with code `1011`; consumers must
treat that as market-data loss, never as broker or account health.

## Consumer configuration

SHAD0W uses `--local-feed-url ws://127.0.0.1:8765`; it reads no data
credentials in that mode and preserves the same Alpaca provider-frame
translation, freshness, causal validation, shadow session, and evidence path.

Thelight uses the explicit fixed setting
`ALPACA_LOCAL_FEED_URL=ws://127.0.0.1:8765`. Only that endpoint (or the
`localhost` spelling) is accepted. This affects `AlpacaMarketSource` only;
its PAPER broker, account reconciliation, order path, and trade-update stream
remain independently fixed to their existing domains.
