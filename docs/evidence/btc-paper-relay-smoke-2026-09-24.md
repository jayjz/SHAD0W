# BTC PAPER relay smoke receipt — 2026-09-24

## Scope

This was a bounded market-data-only smoke of the shared crypto relay at commit
`f63da9c6ecb77de01783f69f3cf035e05b8f0147` plus the uncommitted relay
compatibility fixes in this branch. No BTC PAPER experiment, broker adapter, or
order-capable command was invoked.

## Sanitized observations

- The relay started on `127.0.0.1:8766` and its initial `/health` response was
  `upstream_state: subscribed`, `connection_attempts: 1`, and `clients: 0`.
- The SHAD0W BTC PAPER relay source connected only to
  `ws://127.0.0.1:8766`; it does not send Alpaca authentication and sends only
  the fixed local BTC/USD trade/quote subscription.
- During a 90-second observation, SHAD0W received at least one
  `CryptoQuote` from the local relay. No `CryptoTrade` arrived in that window.
- Final `/health` remained `upstream_state: subscribed` with
  `connection_attempts: 1` and a non-null `last_provider_frame_at`.
- `ss` showed one relay process listening only on `127.0.0.1:8766`.

## Safety result

- One relay-owned upstream Alpaca crypto connection: observed by the relay's
  single connection attempt; no second SHAD0W provider connection was opened.
- Direct-provider fallback: none. Relay mode accepts only the fixed localhost
  URL and the application relay source has no Alpaca authentication path.
- Broker POST: none. The smoke did not construct a broker or invoke an
  experiment/dispatch command; PAPER broker credentials were absent.

## Limitation

This receipt establishes live relay connectivity and quote delivery, not live
trade delivery. The regression suite covers both fixed local trade/quote
channels; a later bounded smoke should capture a `CryptoTrade` before treating
both live channels as observed-provider evidence.
