"""Durable raw BTC history, with a journal-anchored append-only evidence file."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from shadow.application.crypto_evidence import canonical_json, decode_event, encode_event
from shadow.domain.crypto_market import CryptoTrade, UtcNanoseconds
from shadow.execution.journal import ExecutionJournal
from shadow.features.btc_trend import BTC, HOUR_NS, CompletedBtcInterval

HISTORICAL = "alpaca:crypto:us:historical-fetch"
LIVE = "alpaca:crypto:us"


class BtcHistory:
    """Replay observation-ordered trades; availability is never backdated.

    Historical retrieval and buffered live receipt can overlap in wall time. An
    interval becomes available at the maximum receipt of its inputs and closing
    boundary. Within either source receipt order must remain monotone. Historical
    rows initialize features only; a fresh trigger needs a wholly live interval.
    Restart opens a new segment; missing capture time is never called live.
    """

    def __init__(self) -> None:
        self.intervals: tuple[CompletedBtcInterval, ...] = ()
        self.last_fresh_end: int | None = None
        self._start: int | None = None
        self._boundary: int | None = None
        self._last: CryptoTrade | None = None
        self._close: CryptoTrade | None = None
        self._available = 0
        self._receipts: dict[str, int] = {}

    def begin(self, *, start_ns: int, live_boundary_ns: int) -> None:
        UtcNanoseconds(start_ns)
        UtcNanoseconds(live_boundary_ns)
        if start_ns % HOUR_NS or start_ns > live_boundary_ns:
            raise ValueError("aligned historical start and explicit live boundary required")
        if self.intervals and start_ns != self.intervals[-1].end_ns:
            raise ValueError("history segment must continue exactly at last completed end")
        self._start = start_ns
        self._boundary = live_boundary_ns
        self._last = self._close = None
        self._receipts = {}
        self._available = 0

    def accept(self, trade: CryptoTrade) -> tuple[CompletedBtcInterval, ...]:
        if self._start is None or self._boundary is None:
            raise ValueError("history segment required")
        source = trade.provenance.source
        observed, received = trade.observation_time.value, trade.availability_time.value
        if trade.instrument != BTC or source not in (HISTORICAL, LIVE):
            raise ValueError("explicit BTC historical/live provenance required")
        if (source == HISTORICAL) != (observed < self._boundary):
            raise ValueError("trade crosses historical/live boundary")
        if received < self._receipts.get(source, 0):
            raise ValueError("source receipt order moved backwards")
        if self._last is not None:
            if trade == self._last:
                return ()
            if (
                observed < self._last.observation_time.value
                or trade.trade_id == self._last.trade_id
            ):
                raise ValueError("late/conflicting trade")
        if observed < self._start:
            raise ValueError("trade targets completed interval")
        self._receipts[source] = received
        self._available = max(self._available, received)
        rows = []
        while observed >= self._start + HOUR_NS:
            row = CompletedBtcInterval(
                self._start,
                self._start + HOUR_NS,
                self._available,
                None if self._close is None else self._close.price,
                None if self._close is None else self._close.trade_id,
            )
            rows.append(row)
            if source == LIVE and self._start >= self._boundary and row.close is not None:
                self.last_fresh_end = row.end_ns
            self._start += HOUR_NS
            self._close = None
        self._last = self._close = trade
        self.intervals += tuple(rows)
        return tuple(rows)


class MarketEvidence:
    """File bytes fsync before their durable journal anchor; torn tails fail closed.

    There is no tail repair or truncation. Even deletion at a valid line boundary
    is detected against the independently persisted journal anchor. The journal's
    account owner also serializes this file; paths are immutable in its records.
    """

    def __init__(self, path: Path, journal: ExecutionJournal) -> None:
        self.path = path.absolute()
        self.journal = journal
        self.history = BtcHistory()
        self.digest = ""
        self.offset = 0
        self.sequence = 0
        self.events: list[dict[str, object]] = []
        anchors = journal.application_events("market")
        if self.path.is_symlink():
            raise ValueError("market evidence symlinks are unsupported")
        if anchors:
            if not self.path.is_file():
                raise ValueError("market evidence missing")
            with self.path.open("rb") as stream:
                for anchor in anchors:
                    if not isinstance(anchor, tuple) or len(anchor) != 3:
                        raise ValueError("invalid market anchor")
                    bound_path, offset, digest = anchor
                    raw = stream.readline()
                    if not raw.endswith(b"\n"):
                        raise ValueError("truncated market evidence")
                    self.offset += len(raw)
                    value = json.loads(raw)
                    if (
                        (bound_path, offset, digest)
                        != (str(self.path), self.offset, hashlib.sha256(raw).hexdigest())
                        or value["previous"] != self.digest
                        or value["sequence"] != self.sequence
                    ):
                        raise ValueError("market evidence integrity conflict")
                    self._apply(value["payload"])
                    self.digest = str(digest)
                    self.sequence += 1
                if stream.read(1):
                    raise ValueError("unanchored market tail; explicit recovery required")
        elif self.path.exists():
            raise ValueError("unbound existing market evidence")

    def _apply(self, payload: dict[str, object]) -> None:
        kind = payload["kind"]
        if kind == "begin":
            start, boundary = payload["start_ns"], payload["live_boundary_ns"]
            if type(start) is not int or type(boundary) is not int:
                raise ValueError("integer boundary required")
            self.history.begin(start_ns=start, live_boundary_ns=boundary)
        elif kind == "trades":
            events = payload["events"]
            if not isinstance(events, list):
                raise ValueError("trade list required")
            for value in events:
                trade = decode_event(value)
                if not isinstance(trade, CryptoTrade):
                    raise ValueError("raw trade required")
                self.history.accept(trade)
        elif kind != "state":
            raise ValueError("unknown market evidence kind")
        self.events.append(payload)

    def append(self, payload: dict[str, object]) -> None:
        self.journal.assert_held()
        raw = (
            canonical_json({"sequence": self.sequence, "previous": self.digest, "payload": payload})
            + "\n"
        ).encode()
        # Validate against a replay clone before touching durable bytes.
        import copy

        clone = copy.deepcopy(self.history)
        old_events = len(self.events)
        try:
            self._apply(payload)
        except Exception:
            self.history = clone
            del self.events[old_events:]
            raise
        with self.path.open("xb" if self.sequence == 0 else "ab") as stream:
            if stream.tell() != self.offset:
                raise ValueError("market file changed outside owner")
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        digest = hashlib.sha256(raw).hexdigest()
        self.journal.append_application_event(
            "market", (str(self.path), self.offset + len(raw), digest)
        )
        self.offset += len(raw)
        self.digest = digest
        self.sequence += 1

    def begin(self, *, start_ns: int, live_boundary_ns: int) -> None:
        self.append({"kind": "begin", "start_ns": start_ns, "live_boundary_ns": live_boundary_ns})

    def trades(self, events: tuple[CryptoTrade, ...]) -> None:
        self.append({"kind": "trades", "events": [encode_event(event) for event in events]})
