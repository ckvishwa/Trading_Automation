from datetime import datetime, timedelta, timezone

import pytest

from app.signals.timeframes import (
    UnsupportedTimeframe,
    bar_close_from_open,
    canonical_timeframe,
    timeframe_duration,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", "1m"),
        ("5", "5m"),
        ("15", "15m"),
        ("60", "60m"),
        ("5m", "5m"),
        ("30S", "30s"),
        ("D", "1D"),
        ("1D", "1D"),
        ("W", "1W"),
    ],
)
def test_canonical_timeframe_maps_tradingview_intervals(raw, expected):
    assert canonical_timeframe(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "banana", "5x", "m5"])
def test_canonical_timeframe_rejects_unrecognised_intervals(raw):
    with pytest.raises(UnsupportedTimeframe):
        canonical_timeframe(raw)


def test_timeframe_duration_for_intraday():
    assert timeframe_duration("5") == timedelta(minutes=5)
    assert timeframe_duration("30S") == timedelta(seconds=30)


def test_daily_and_higher_timeframes_have_no_derivable_bar_duration():
    # A daily bar's length depends on the session calendar, so deriving its
    # close time from its open time would be a fabrication.
    for raw in ("D", "W", "M"):
        with pytest.raises(UnsupportedTimeframe):
            timeframe_duration(raw)


def test_bar_close_is_open_plus_one_bar():
    # TradingView's {{time}} is the bar OPEN. A 5m bar opening at 04:35Z closes
    # at 04:40Z, which is when a Once Per Bar Close alert fires.
    bar_open = datetime(2026, 10, 20, 4, 35, tzinfo=timezone.utc)
    assert bar_close_from_open(bar_open, "5") == datetime(
        2026, 10, 20, 4, 40, tzinfo=timezone.utc
    )
