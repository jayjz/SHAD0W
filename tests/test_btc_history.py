"""Raw historical/live parity, causality and journal-anchored restart evidence."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from shadow.application.btc_history import HISTORICAL, LIVE, BtcHistory, MarketEvidence
from shadow.domain.crypto_market import CryptoTrade, TakerSide, UtcNanoseconds
from shadow.domain.market import AvailabilitySemantics, Provenance
from shadow.execution.journal import ExecutionJournal
from shadow.features.btc_trend import BTC, HOUR_NS, BtcIntervals, CompletedBtcInterval
from shadow.strategies.btc_trend import engineering_canary
from tests.test_btc_journal import journal as journal


def trade(hour: int, *, source: str = HISTORICAL, received: int = 80 * HOUR_NS) -> CryptoTrade:
    return CryptoTrade(
        BTC,
        Decimal(100 + hour),
        Decimal(1),
        str(hour),
        TakerSide.BUY,
        UtcNanoseconds(hour * HOUR_NS),
        UtcNanoseconds(received),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance(source, "UTC"),
    )


def test_raw_73_intervals_same_as_c1_and_fresh_boundary() -> None:
    history = BtcHistory()
    history.begin(start_ns=0, live_boundary_ns=74 * HOUR_NS)
    c1 = BtcIntervals(started_ns=0)
    expected: tuple[CompletedBtcInterval, ...] = ()
    for hour in range(74):
        event = trade(hour)
        history.accept(event)
        expected += c1.accept(event)
    assert len(history.intervals) == 73
    assert history.intervals == expected
    assert history.last_fresh_end is None
    config = engineering_canary()
    assert config.features(history.intervals, 79 * HOUR_NS) is None
    assert config.features(history.intervals, 80 * HOUR_NS) is not None
    # Buffered live receipt can precede completion of historical fetch; neither is backdated.
    history.accept(trade(74, source=LIVE, received=74 * HOUR_NS))
    assert history.last_fresh_end is None
    history.accept(trade(75, source=LIVE, received=75 * HOUR_NS))
    assert history.last_fresh_end == 75 * HOUR_NS
    assert history.intervals[-1].available_ns == 80 * HOUR_NS


def test_restart_reproduces_features_and_detects_tail_loss(
    journal: ExecutionJournal,
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.jsonl"
    store = MarketEvidence(path, journal)
    store.begin(start_ns=0, live_boundary_ns=74 * HOUR_NS)
    store.trades(tuple(trade(hour) for hour in range(74)))
    store.trades((trade(74, source=LIVE), trade(75, source=LIVE)))
    restored = MarketEvidence(path, journal)
    assert restored.history.intervals == store.history.intervals
    assert restored.history.last_fresh_end == store.history.last_fresh_end
    config = engineering_canary()
    assert config.features(restored.history.intervals, 80 * HOUR_NS) == config.features(
        store.history.intervals, 80 * HOUR_NS
    )
    raw = path.read_bytes()
    path.write_bytes(b"\n".join(raw.splitlines()[:-1]) + b"\n")
    with pytest.raises(ValueError, match="truncated"):
        MarketEvidence(path, journal)


@pytest.mark.parametrize("corruption", ["edit", "tail", "missing"])
def test_market_integrity(journal: ExecutionJournal, tmp_path: Path, corruption: str) -> None:
    path = tmp_path / "market.jsonl"
    store = MarketEvidence(path, journal)
    store.begin(start_ns=0, live_boundary_ns=74 * HOUR_NS)
    if corruption == "edit":
        path.write_bytes(path.read_bytes().replace(b'"start_ns":0', b'"start_ns":1'))
    elif corruption == "tail":
        with path.open("ab") as stream:
            stream.write(b"{}\n")
    else:
        path.unlink()
    with pytest.raises(ValueError):
        MarketEvidence(path, journal)


def test_gap_and_invalid_order_fail_closed() -> None:
    history = BtcHistory()
    history.begin(start_ns=0, live_boundary_ns=74 * HOUR_NS)
    for hour in range(74):
        if hour != 35:
            history.accept(trade(hour))
    assert history.intervals[35].close is None
    assert engineering_canary().features(history.intervals, 80 * HOUR_NS) is None
    with pytest.raises(ValueError):
        history.accept(trade(72))
    with pytest.raises(ValueError):
        history.accept(replace(trade(74), provenance=Provenance(HISTORICAL)))


def test_historical_adapter_pages_exact_ns_and_receipt() -> None:
    import json
    from collections.abc import Mapping

    from shadow.adapters.alpaca.btc_history import historical_trades
    from shadow.adapters.alpaca.crypto_stream import CryptoDataCredentials
    from shadow.adapters.alpaca.paper_broker import HttpResponse

    class Transport:
        calls = 0

        def request(
            self,
            *,
            method: str,
            url: str,
            headers: Mapping[str, str],
            body: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            assert method == "GET" and body is None
            assert "bars" not in url and "sort=asc" in url
            hour = self.calls
            self.calls += 1
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "trades": {
                            "BTC/USD": [
                                {
                                    "t": f"1970-01-01T0{hour}:00:00.000000001Z",
                                    "i": hour,
                                    "p": "100",
                                    "s": "1",
                                    "tks": "B",
                                }
                            ]
                        },
                        "next_page_token": "page2" if hour == 0 else None,
                    }
                ).encode(),
            )

    transport = Transport()
    pages = tuple(
        historical_trades(
            transport=transport,
            credentials=CryptoDataCredentials("key", "secret"),
            start_ns=0,
            end_ns=2 * HOUR_NS,
            now_ns=lambda: 3 * HOUR_NS,
            remaining_seconds=lambda: 10,
        )
    )
    assert transport.calls == 2
    assert pages[0][0].observation_time.value == 1
    assert pages[1][0].availability_time.value == 3 * HOUR_NS
    assert pages[1][0].provenance.source == HISTORICAL
