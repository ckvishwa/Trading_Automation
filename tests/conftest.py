"""Shared fixtures.

Every test runs fully offline: no TradingView, no Kite, no network, no open
market session. Timestamps are supplied explicitly so that session and
freshness logic is deterministic rather than dependent on when the suite runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings

# Tuesday 2026-10-20, 10:10 IST == 04:40 UTC. Used as "now" for ingress tests.
NOW = datetime(2026, 10, 20, 4, 40, 30, tzinfo=timezone.utc)
TEST_TOKEN = "t" * 48
INDICATOR = "Half Trend Regression [AlgoAlpha]"


def alert_payload(**overrides) -> dict:
    """A valid 5-minute bearish HTR reversal alert body, field-for-field.

    `bar_open_time` is 04:35Z, so the 5m bar closes at 04:40Z, which is when a
    `Once Per Bar Close` alert fires.
    """
    payload = {
        "indicator": INDICATOR,
        "indicator_version": "A9Dp4WrK",
        "event": "REVERSAL",
        "signal": "SELL",
        "exchange": "NSE",
        "ticker": "NIFTY",
        "interval": "5",
        "signal_price": "24000.5",
        "bar_open_time": "2026-10-20T04:35:00Z",
        "fired_at": "2026-10-20T04:40:00Z",
        "mode": "PAPER",
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _OMIT}


class _Omit:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<omit>"


_OMIT = _Omit()
OMIT = _OMIT


@pytest.fixture
def calendar_file(tmp_path: Path) -> Path:
    """A calendar verified through 2026, with one known holiday and no F&O entry.

    The CASH/FO split is deliberate: the two segments do not share a holiday
    list in reality, and the code must not assume they do.
    """
    path = tmp_path / "nse_calendar.json"
    path.write_text(
        json.dumps(
            {
                "timezone": "Asia/Kolkata",
                "verified_through": "2026-12-31",
                "segments": {
                    "CASH": {
                        "regular_open": "09:15",
                        "regular_close": "15:30",
                        "pre_open_start": "09:00",
                        "pre_open_end": "09:08",
                    },
                    "FO": {"regular_open": "09:15", "regular_close": "15:30"},
                },
                "trading_holidays": {"CASH": ["2026-10-21"], "FO": []},
                "special_sessions": [],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def unverified_calendar_file(tmp_path: Path) -> Path:
    path = tmp_path / "unverified_calendar.json"
    path.write_text(
        json.dumps(
            {
                "timezone": "Asia/Kolkata",
                "verified_through": None,
                "segments": {
                    "CASH": {"regular_open": "09:15", "regular_close": "15:30"},
                    "FO": {"regular_open": "09:15", "regular_close": "15:30"},
                },
                "trading_holidays": {"CASH": [], "FO": []},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings(tmp_path: Path, calendar_file: Path) -> Settings:
    return Settings(
        TRADING_MODE="PAPER",
        WEBHOOK_TOKEN=TEST_TOKEN,
        EXPECTED_INDICATOR=INDICATOR,
        ALLOWED_SYMBOLS="NSE:NIFTY",
        ALLOWED_TIMEFRAMES="1m,3m,5m,15m",
        MAX_SIGNAL_AGE_SECONDS=120,
        CLOCK_SKEW_TOLERANCE_SECONDS=10,
        DATABASE_PATH=tmp_path / "engine.db",
        HOLIDAY_FILE=calendar_file,
        _env_file=None,
    )
