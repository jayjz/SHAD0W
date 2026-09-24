"""Bounded BTC PAPER application composition, entirely with the existing fake broker."""

from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from shadow.application.btc_history import HISTORICAL, LIVE, MarketEvidence
from shadow.application.crypto_paper import BtcPaperExperiment, HistoricalSource, LiveSource
from shadow.domain.crypto_market import CryptoQuote, CryptoTrade, TakerSide, UtcNanoseconds
from shadow.domain.market import AvailabilitySemantics, Provenance
from shadow.execution.btc_journal import BtcJournal
from shadow.execution.journal import ExecutionJournal
from shadow.execution.ownership import AccountOwner
from shadow.features.btc_trend import BTC, HOUR_NS
from shadow.risk.models import OperatorControls
from tests.test_btc_dispatch import FakeBtcBroker
from tests.test_btc_journal import authority
from tests.test_paper_reconciliation import NOW


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[ExecutionJournal]:
    """The existing BTC journal fixture's account-owned topology."""
    owner_directory = tmp_path / "owner"
    owner_directory.mkdir()
    with AccountOwner.acquire(
        ownership_directory=owner_directory, account_id="paper-account"
    ) as owner:
        with ExecutionJournal.create(
            path=tmp_path / "journal.sqlite",
            owner=owner,
            account_id="paper-account",
            operational_scope="scope",
            created_at=NOW,
        ) as value:
            yield value


def _trade(ns: int, price: Decimal, source: str, received: int) -> CryptoTrade:
    return CryptoTrade(
        BTC,
        price,
        Decimal("1"),
        str(ns),
        TakerSide.BUY,
        UtcNanoseconds(ns),
        UtcNanoseconds(received),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance(source, "UTC"),
    )


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _FreshFake(FakeBtcBroker):
    """Existing transport fake with account/asset evidence sampled at the test cut."""

    def __init__(self, *args: object, clock: _Clock) -> None:
        super().__init__(*args)  # type: ignore[arg-type]
        self.clock = clock

    def read_btc_account(self):  # type: ignore[no-untyped-def]
        account = super().read_btc_account()
        return replace(
            account,
            evidence=replace(
                account.evidence,
                observation_time=self.clock(),
                availability_time=self.clock(),
            ),
        )

    def read_btc_asset(self):  # type: ignore[no-untyped-def]
        asset = super().read_btc_asset()
        return replace(
            asset,
            evidence=replace(
                asset.evidence,
                observation_time=self.clock(),
                availability_time=self.clock(),
            ),
        )


def _runner(
    journal: ExecutionJournal, tmp_path: Path
) -> tuple[BtcPaperExperiment, _FreshFake, _Clock]:
    config, attempt = authority()
    clock = _Clock(NOW)
    store = BtcJournal(journal)
    broker = _FreshFake(store, attempt, clock=clock)
    policy = attempt.evaluation.policy
    runner = BtcPaperExperiment(
        broker=broker,
        journal=journal,
        market_evidence=MarketEvidence(tmp_path / "market.jsonl", journal),
        run_config=replace(config, maximum_per_run=1, maximum_per_period=1),
        risk_policy=policy,
        quantity=attempt.request.quantity,
        controls=lambda: OperatorControls(True, False, clock(), clock()),
        now=clock,
    )
    return runner, broker, clock


def test_preflight_performs_no_post(journal: ExecutionJournal, tmp_path: Path) -> None:
    runner, broker, _ = _runner(journal, tmp_path)
    status = runner.preflight()
    assert broker.posts == 0
    assert status.broker_ready
    assert status.submission_budget == 1


def test_historical_context_cannot_trigger_before_wholly_live_interval(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = _runner(journal, tmp_path)
    boundary = (int(clock().timestamp() * 1_000_000_000) // HOUR_NS) * HOUR_NS
    start = boundary - 74 * HOUR_NS

    def historical(_start: int, _end: int) -> Iterator[tuple[CryptoTrade, ...]]:
        yield tuple(
            _trade(value, Decimal(100 + index), HISTORICAL, boundary)
            for index, value in enumerate(range(start, boundary, HOUR_NS))
        )

    quote = CryptoQuote(
        BTC,
        Decimal("172"),
        Decimal("173"),
        Decimal("1"),
        Decimal("1"),
        UtcNanoseconds(boundary),
        UtcNanoseconds(boundary),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance(LIVE, "UTC"),
    )
    source: HistoricalSource = historical

    def live(_duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        return quote, _trade(boundary, Decimal(174), LIVE, boundary)

    live_source: LiveSource = live
    result = runner.experiment(
        source,
        live_source,
        1,
    )
    assert result.stop_reason == "LIVE_INTERVAL_TIMEOUT"
    assert broker.posts == 0


def test_no_signal_after_fresh_live_interval_never_posts(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = _runner(journal, tmp_path)
    boundary = (int(clock().timestamp() * 1_000_000_000) // HOUR_NS) * HOUR_NS
    start = boundary - 74 * HOUR_NS

    def historical(_start: int, _end: int) -> Iterator[tuple[CryptoTrade, ...]]:
        yield tuple(
            _trade(value, Decimal(200 - index), HISTORICAL, boundary)
            for index, value in enumerate(range(start, boundary, HOUR_NS))
        )

    quote = CryptoQuote(
        BTC,
        Decimal("100"),
        Decimal("101"),
        Decimal("1"),
        Decimal("1"),
        UtcNanoseconds(boundary + HOUR_NS + 1),
        UtcNanoseconds(boundary + HOUR_NS + 1),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance(LIVE, "UTC"),
    )

    def live(_duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        clock.value = datetime.fromtimestamp((boundary + HOUR_NS) / 1_000_000_000, tz=UTC) + (
            datetime.resolution
        )
        return (
            quote,
            _trade(boundary, Decimal(125), LIVE, boundary),
            _trade(boundary + HOUR_NS, Decimal(124), LIVE, boundary + HOUR_NS + 1),
        )

    source = historical
    live_source = live
    result = runner.experiment(source, live_source, 1)
    assert result.stop_reason == "NO_SIGNAL"
    assert broker.posts == 0


def _rising_sources(
    clock: _Clock,
) -> tuple[HistoricalSource, LiveSource]:
    boundary = (int(clock().timestamp() * 1_000_000_000) // HOUR_NS) * HOUR_NS
    start = boundary - 74 * HOUR_NS

    def historical(_start: int, _end: int) -> Iterator[tuple[CryptoTrade, ...]]:
        yield tuple(
            _trade(value, Decimal(100 + index), HISTORICAL, boundary)
            for index, value in enumerate(range(start, boundary, HOUR_NS))
        )

    def live(_duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        clock.value = datetime.fromtimestamp((boundary + HOUR_NS) / 1_000_000_000, tz=UTC) + (
            datetime.resolution
        )
        return (
            CryptoQuote(
                BTC,
                Decimal("174"),
                Decimal("175"),
                Decimal("1"),
                Decimal("1"),
                UtcNanoseconds(boundary + HOUR_NS + 1_000),
                UtcNanoseconds(boundary + HOUR_NS + 1_000),
                AvailabilitySemantics.SYSTEM_RECEIVED,
                Provenance(LIVE, "UTC"),
            ),
            _trade(boundary, Decimal(174), LIVE, boundary),
            _trade(boundary + HOUR_NS, Decimal(175), LIVE, boundary + HOUR_NS + 1_000),
        )

    return historical, live


def test_risk_rejection_after_fresh_live_interval_never_posts(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = _runner(journal, tmp_path)
    broker.attempt = replace(
        broker.attempt,
        revalidation=replace(
            broker.attempt.revalidation,
            account=replace(broker.attempt.revalidation.account, available_cash=Decimal(0)),
        ),
    )
    historical, live = _rising_sources(clock)
    result = runner.experiment(historical, live, 1)
    assert result.stop_reason == "RISK_REJECTED"
    assert broker.posts == 0


def test_authorized_experiment_posts_once_then_stops(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = _runner(journal, tmp_path)
    broker.attempt = replace(
        broker.attempt,
        revalidation=replace(
            broker.attempt.revalidation,
            activities=replace(
                broker.attempt.revalidation.activities,
                history_verified=False,
                coverage_reference=None,
            ),
        ),
    )
    historical, live = _rising_sources(clock)
    result = runner.experiment(historical, live, 1)
    assert result.stop_reason == "POST_ATTEMPT_OBSERVED"
    assert broker.posts == 1
    assert not result.preflight.proof_ready
    assert result.preflight.strict_lifecycle == "unresolved"
    repeat = runner.experiment(historical, live, 1)
    assert repeat.stop_reason == "SUBMISSION_BUDGET_SPENT"
    assert broker.posts == 1
