"""Replay CLI: `python -m app.research replay --jsonl FILE`.

Exists so replay is runnable, not merely importable -- a research path that can
only be exercised from a test is a research path that quietly rots.

Replay uses the engine's own adapter and event store, so a replayed event is
validated by exactly the code the live webhook uses. `--dry-run` writes to a
temporary database so a file can be checked without touching the real journal.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from app.config import get_settings
from app.logging_setup import configure_logging
from app.market_data.providers.upstox import ProtobufFeedDecoder
from app.market_data.session import Segment, SessionCalendar
from app.research.replay import SignalReplay
from app.signals.htr_adapter import policy_from_settings
from app.storage.event_store import EventStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.research")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay", help="replay recorded alert payloads")
    replay.add_argument("--jsonl", required=True, type=Path, help="one alert payload per line")
    replay.add_argument(
        "--dry-run",
        action="store_true",
        help="use a throwaway database instead of the configured one",
    )
    replay.add_argument(
        "--delivery-delay-seconds",
        type=float,
        default=1.0,
        help="modelled gap between an alert firing and the engine receiving it",
    )

    market = subparsers.add_parser(
        "market-replay", help="replay recorded Protobuf market-data frames"
    )
    market.add_argument(
        "--frames",
        required=True,
        type=Path,
        help="length-prefixed Protobuf frames, e.g. examples/fixtures/upstox-feed-session.binpb",
    )
    market.add_argument("--symbol", default="NSE:NIFTY")
    market.add_argument("--timeframe", default="5m")
    market.add_argument(
        "--dry-run",
        action="store_true",
        help="use a throwaway database instead of the configured one",
    )

    args = parser.parse_args(argv)
    configure_logging()
    settings = get_settings()

    from datetime import timedelta

    calendar = SessionCalendar.load(settings.holiday_file)
    policy = policy_from_settings(
        settings, session_open_ist=calendar.segment_open(Segment.FO)
    )

    if args.dry_run:
        database_path = Path(tempfile.mkdtemp(prefix="replay-")) / "replay.db"
    else:
        database_path = settings.database_path

    if args.command == "market-replay":
        from datetime import datetime, timezone

        from app.research.market_replay import MarketDataReplay

        store = EventStore(database_path)
        try:
            engine = MarketDataReplay.for_calendar(
                store,
                calendar,
                symbol=args.symbol,
                timeframe=args.timeframe,
                on=datetime.now(tz=timezone.utc),
                segment=Segment.FO,
            )
            summary = engine.run_file(args.frames)
            stored = store.count_candles()
        finally:
            store.close()
        print(
            json.dumps(
                {
                    "database": str(database_path),
                    "dry_run": bool(args.dry_run),
                    "mode": settings.trading_mode,
                    "live_orders": False,
                    "frames_file": str(args.frames),
                    "schema_sha256": ProtobufFeedDecoder().schema_lock["sha256"],
                    **summary.as_dict(),
                    "candles_in_store": stored,
                    "builder_stats": vars(engine.stats),
                },
                indent=2,
            )
        )
        return 0

    store = EventStore(database_path)
    try:
        engine = SignalReplay(
            store,
            policy,
            calendar,
            delivery_delay=timedelta(seconds=args.delivery_delay_seconds),
        )
        summary = engine.run_jsonl(args.jsonl)
    finally:
        store.close()

    print(
        json.dumps(
            {
                "database": str(database_path),
                "dry_run": bool(args.dry_run),
                "mode": settings.trading_mode,
                "live_orders": False,
                **summary.as_dict(),
            },
            indent=2,
        )
    )
    # A replay that rejected everything is a result, not a crash.
    return 0


if __name__ == "__main__":
    sys.exit(main())
