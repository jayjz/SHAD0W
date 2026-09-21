"""Command-line entry point for bounded Alpaca market-data shadow sessions."""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from shadow.adapters.alpaca.normalize import SUPPORTED_FEEDS, symbols_checked
from shadow.adapters.alpaca.stream import DataCredentials, run_live
from shadow.application.evidence import EvidenceWriter
from shadow.application.shadow import ShadowConfig, ShadowSession
from shadow.domain import Instrument
from shadow.risk import OperationalQuantityConfig, RiskPolicy
from shadow.strategies import MeanReversionConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SHADOW MODE: Alpaca live market data; no order submission"
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument(
        "--symbol", action="append", required=True, help="uppercase equity symbol; repeat"
    )
    parser.add_argument("--evidence-path", type=Path, required=True)
    parser.add_argument(
        "--feed", choices=SUPPORTED_FEEDS, default=os.environ.get("ALPACA_DATA_FEED", "iex")
    )
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument(
        "--local-feed-url",
        help=(
            "explicit localhost-only Alpaca provider-frame relay URL; no data credentials are read"
        ),
    )
    parser.add_argument("--rolling-window", type=int, default=20)
    parser.add_argument("--entry-threshold", type=Decimal, default=Decimal("-2"))
    parser.add_argument("--exit-threshold", type=Decimal, default=Decimal("0"))
    parser.add_argument("--quantity", type=Decimal, default=Decimal("1"))
    parser.add_argument("--maximum-bar-age-seconds", type=int, default=300)
    parser.add_argument("--maximum-quote-age-seconds", type=int, default=15)
    parser.add_argument(
        "--source-dataset-id",
        help="stable feed/dataset lineage; required when evidence may be prepared for PAPER",
    )
    parser.add_argument(
        "--strategy-configuration-id",
        help="stable strategy configuration identity; required for PAPER preparation",
    )
    parser.add_argument(
        "--quantity-configuration-id",
        help="stable operational quantity identity; required for PAPER preparation",
    )
    return parser


def _config(arguments: argparse.Namespace) -> ShadowConfig:
    symbols = symbols_checked(tuple(arguments.symbol))
    instruments = tuple(Instrument(symbol) for symbol in symbols)
    feature_age = timedelta(seconds=arguments.maximum_bar_age_seconds)
    strategies = tuple(
        MeanReversionConfig(
            instrument=instrument,
            configuration_id=(
                arguments.strategy_configuration_id
                or f"{arguments.session_id}:{instrument.identifier}:mean-reversion-v1"
            ),
            rolling_window=arguments.rolling_window,
            entry_threshold=arguments.entry_threshold,
            exit_threshold=arguments.exit_threshold,
            maximum_feature_age=feature_age,
        )
        for instrument in instruments
    )
    quantities = tuple(
        OperationalQuantityConfig(
            instrument,
            arguments.quantity_configuration_id
            or f"{arguments.session_id}:{instrument.identifier}:quantity-v1",
            arguments.quantity,
        )
        for instrument in instruments
    )
    return ShadowConfig(
        session_id=arguments.session_id,
        code_revision=arguments.code_revision,
        strategies=strategies,
        quantities=quantities,
        risk_policy=RiskPolicy(
            policy_id=f"{arguments.session_id}:shadow-observability-v1",
            enabled=True,
            allowed_instruments=instruments,
            maximum_quantity_per_order=arguments.quantity,
            maximum_concurrent_positions=1,
            maximum_signal_age=feature_age,
            maximum_feature_age=feature_age,
            maximum_quote_age=timedelta(seconds=arguments.maximum_quote_age_seconds),
            maximum_operational_state_age=timedelta(seconds=1),
        ),
        source=f"alpaca:{arguments.feed}",
        maximum_bar_age=feature_age,
        maximum_quote_age=timedelta(seconds=arguments.maximum_quote_age_seconds),
        source_dataset_id=arguments.source_dataset_id,
    )


def main() -> int:
    arguments = _parser().parse_args()
    config = _config(arguments)
    credentials = (
        None
        if arguments.local_feed_url is not None
        else DataCredentials.from_environment(os.environ)
    )
    print("SHADOW MODE — NO ORDER SUBMISSION")
    writer = EvidenceWriter(arguments.evidence_path, config)
    try:
        asyncio.run(
            run_live(
                ShadowSession(config),
                credentials,
                writer,
                duration=arguments.duration_seconds,
                feed=arguments.feed,
                local_feed_url=arguments.local_feed_url,
            )
        )
    finally:
        writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
