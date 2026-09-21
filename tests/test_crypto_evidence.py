from decimal import Decimal
from pathlib import Path

import pytest

from shadow.application.crypto_evidence import (
    CryptoCaptureStatus,
    CryptoEvidenceError,
    CryptoEvidenceHeader,
    CryptoEvidenceWriter,
)
from shadow.application.crypto_replay import replay
from shadow.application.crypto_session import CryptoSession
from shadow.domain.crypto_market import BookAction, BookLevel, CryptoBookEvent, UtcNanoseconds
from shadow.domain.market import AvailabilitySemantics, Instrument, Provenance


def book(action: BookAction, price: str, time: int) -> CryptoBookEvent:
    return CryptoBookEvent(
        Instrument("BTC/USD"),
        action,
        (BookLevel(Decimal(price), Decimal("1")),),
        (),
        UtcNanoseconds(time),
        UtcNanoseconds(time + 1),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("alpaca:crypto:us", "UTC"),
    )


def header() -> CryptoEvidenceHeader:
    return CryptoEvidenceHeader("test", "deadbeef", {"reducer": "1"}, {"decimal": "exact"})


def test_capture_replays_reset_update_and_hashes(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    session = CryptoSession()
    session.establish_connection()
    writer = CryptoEvidenceWriter(path, header())
    first = writer.append(
        session.process(book(BookAction.RESET, "100", 1)), frame_sequence=0, element_index=0
    )
    writer.append(
        session.process(book(BookAction.UPDATE, "101", 2)), frame_sequence=1, element_index=0
    )
    writer.close()
    result = replay(path)
    assert first["sequence"] == 0
    assert result.status is CryptoCaptureStatus.COMPLETE_STOPPED
    assert result.btc is not None
    assert result.btc.bids[0].price == Decimal("101")
    assert len(result.digest) == 64


def test_writer_is_exclusive_and_missing_terminal_is_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    writer = CryptoEvidenceWriter(path, header())
    writer._file.close()  # exercise an interrupted capture without a terminal record
    with pytest.raises(FileExistsError):
        CryptoEvidenceWriter(path, header())
    assert replay(path).status is CryptoCaptureStatus.INCOMPLETE


def test_replay_rejects_tampered_record_hash(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    session = CryptoSession()
    session.establish_connection()
    writer = CryptoEvidenceWriter(path, header())
    writer.append(
        session.process(book(BookAction.RESET, "100", 1)), frame_sequence=0, element_index=0
    )
    writer.close()
    content = path.read_text(encoding="utf-8").replace('"record_hash":"', '"record_hash":"x', 1)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(CryptoEvidenceError, match="bad record hash"):
        replay(path)
