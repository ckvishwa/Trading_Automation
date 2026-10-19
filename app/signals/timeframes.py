"""TradingView interval parsing and bar-time normalization.

TradingView's `{{interval}}` placeholder is a bare resolution string: "1", "5",
"60" for minutes, "D"/"W"/"M" for higher resolutions, "1S" style for seconds.
TradingView's `{{time}}` placeholder is the **opening** time of the bar that
triggered the alert, not its close. Treating a bar-open timestamp as a close
timestamp silently backdates every signal by one bar, so the conversion is
explicit and tested here rather than inlined at call sites.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_MINUTES_ONLY = re.compile(r"^(\d+)$")
_SECONDS = re.compile(r"^(\d+)S$", re.IGNORECASE)
_DAILY_PLUS = {"D": "1D", "1D": "1D", "W": "1W", "1W": "1W", "M": "1M", "1M": "1M"}


class UnsupportedTimeframe(ValueError):
    """Raised when an interval string cannot be mapped to a canonical timeframe."""


def canonical_timeframe(raw: str) -> str:
    """Map a TradingView interval string to a canonical timeframe label.

    "5" -> "5m", "60" -> "60m", "30S" -> "30s", "D" -> "1D".
    Already-canonical labels ("5m") are accepted so replayed internal events
    round-trip unchanged.
    """
    value = str(raw).strip()
    if not value:
        raise UnsupportedTimeframe("empty interval")

    upper = value.upper()
    if upper in _DAILY_PLUS:
        return _DAILY_PLUS[upper]

    if match := _MINUTES_ONLY.match(upper):
        return f"{int(match.group(1))}m"
    if match := _SECONDS.match(upper):
        return f"{int(match.group(1))}s"

    lower = value.lower()
    if re.fullmatch(r"\d+[ms]", lower):
        return lower
    if re.fullmatch(r"\d+[DWM]", upper):
        return upper

    raise UnsupportedTimeframe(f"unrecognised interval: {raw!r}")


def timeframe_duration(timeframe: str) -> timedelta:
    """Duration of one bar of a canonical intraday timeframe.

    Daily and higher timeframes have session-dependent lengths and are therefore
    rejected: their close time cannot be derived from the open time alone.
    """
    canonical = canonical_timeframe(timeframe)
    if canonical.endswith("m"):
        return timedelta(minutes=int(canonical[:-1]))
    if canonical.endswith("s"):
        return timedelta(seconds=int(canonical[:-1]))
    raise UnsupportedTimeframe(
        f"bar duration of {canonical!r} depends on the session calendar; "
        "intraday timeframes only in V1"
    )


def bar_close_from_open(bar_open: datetime, timeframe: str) -> datetime:
    """Close timestamp of the bar that opened at `bar_open`.

    The close time is the exclusive end of the bar interval, which is the
    instant a `Once Per Bar Close` alert is evaluated.
    """
    return bar_open + timeframe_duration(timeframe)
