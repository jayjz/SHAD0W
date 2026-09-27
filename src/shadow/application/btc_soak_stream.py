"""Soak-only heartbeat driver around the existing normalized live source.

The source retains socket/protocol/receipt ownership. Unlike the strict session's
blocking iterator, this driver yields a heartbeat during silence so the soak
owner can poll the broker, observe controls, and enforce its monotonic deadline.
"""

import asyncio
from collections.abc import Generator

from shadow.application.crypto_paper import PaperApplicationError, _DirectLive
from shadow.domain.crypto_market import CryptoQuote, CryptoTrade


def heartbeat_events(source: _DirectLive) -> Generator[CryptoTrade | CryptoQuote | None]:
    try:
        source._open()
        assert source._loop is not None and source._socket is not None
        while source._remaining() > 0:
            if source._pending:
                yield source._pending.popleft()
                continue
            try:
                raw = source._loop.run_until_complete(
                    asyncio.wait_for(source._socket.recv(), timeout=min(5, source._remaining()))
                )
            except TimeoutError:
                yield None
                continue
            if not isinstance(raw, (str, bytes)):
                raise PaperApplicationError("crypto stream returned invalid frame")
            source._collect(raw)
    finally:
        source.close()
