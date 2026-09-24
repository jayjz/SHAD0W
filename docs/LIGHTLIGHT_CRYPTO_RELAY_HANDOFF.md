# LIGHTLIGHT crypto relay handoff

The sibling repository is intentionally not modified by this SHAD0W branch:
the declared writable workspace covers SHAD0W only. Apply this change in a
separate LIGHTLIGHT branch after the SHAD0W relay branch is available.

1. In `src/lib/lightlight/alpaca-crypto.server.ts`, add a validated optional
   `ALPACA_CRYPTO_LOCAL_FEED_URL` configuration accepting only
   `ws://127.0.0.1:8766` and `ws://localhost:8766`. In relay mode create that
   socket, send only `{"action":"subscribe","bars":["BTC/USD"]}`, require
   its exact acknowledgement, and do not send an Alpaca `connected`/`auth`
   handshake. Keep direct mode explicit and unchanged when the variable is
   absent. Relay setup/error/close must fail closed, never construct the Alpaca
   URL as a fallback.
2. In `scripts/btc-market-worker.ts`, build `AlpacaCryptoMarketSource` from
   the relay configuration first. Require Alpaca credentials only for the
   unchanged `AlpacaCryptoHistoricalBarsClient` and direct-stream mode. Preserve
   the existing `brokerAuthority: "NONE"` and read-only durable capability.
3. Add `ALPACA_CRYPTO_LOCAL_FEED_URL=ws://127.0.0.1:8766` (non-secret) to
   LIGHTLIGHT `.env.example`, and extend its relay capability document with the
   crypto-specific endpoint rather than overloading `ALPACA_LOCAL_FEED_URL`.
4. In `alpaca-crypto.test.ts` and `btc-runtime.test.ts`, add cases proving
   relay URL selection, no auth payload, exact BTC/USD subscription, explicit
   control/error failure, no direct fallback, `closedBarFromAlpacaCrypto`
   validation, and `brokerAuthority === "NONE"`. Retain the existing direct
   406/reconnect tests. In historical recovery tests, prove REST bootstrap and
   strict exact gap recovery remain independent.
5. Verify from LIGHTLIGHT with `ALPACA_CRYPTO_LOCAL_FEED_URL=` cleared for
   direct-mode tests: `npm run typecheck`, `npm run lint`,
   `ALPACA_LOCAL_FEED_URL= npm run test:lightlight`, `npm run build`, and
   `git diff --check`.
