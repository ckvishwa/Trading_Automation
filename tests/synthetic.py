"""Synthetic candle series for offline indicator and replay tests.

Deterministic by construction: no randomness, no wall-clock dependence. The
price paths here are shaped to exercise HTR's state machine (a rise, a fall, a
second rise), not to resemble NIFTY.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.market_data.contracts import Candle, CandleSeries, Provenance

SYMBOL = "NSE:NIFTY"
TIMEFRAME = "5m"
BAR = timedelta(minutes=5)
# 03:45Z == 09:15 IST, the first bar of an NSE session.
FIRST_OPEN = datetime(2026, 10, 20, 3, 45, tzinfo=timezone.utc)

FIXTURE_PROVENANCE = Provenance(
    provider="synthetic",
    fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    endpoint="tests/synthetic.py",
    is_mock=True,
)


def candles_from_closes(
    closes: Sequence[float],
    *,
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    bar: timedelta = BAR,
    first_open: datetime = FIRST_OPEN,
    wick: float = 1.0,
) -> list[Candle]:
    """One candle per close price, with a fixed symmetric wick.

    Open is the previous close, so the series is continuous and the OHLC
    invariants hold without any special-casing.
    """
    bars: list[Candle] = []
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        high = max(open_price, close) + wick
        low = min(open_price, close) - wick
        open_time = first_open + index * bar
        bars.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=open_time + bar,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=1_000 + index,
                provenance=FIXTURE_PROVENANCE,
            )
        )
    return bars


def series_from_closes(closes: Sequence[float], **kwargs) -> CandleSeries:
    bars = candles_from_closes(closes, **kwargs)
    return CandleSeries(
        symbol=kwargs.get("symbol", SYMBOL),
        timeframe=kwargs.get("timeframe", TIMEFRAME),
        candles=tuple(bars),
    )


def leg(start: float, end: float, steps: int) -> list[float]:
    """A straight price leg from `start` to `end` over `steps` bars."""
    if steps <= 0:
        return []
    step = (end - start) / steps
    return [start + step * (i + 1) for i in range(steps)]


def zigzag_closes(
    base: float = 24_000.0,
    *,
    rise: float = 120.0,
    fall: float = 160.0,
    leg_bars: int = 14,
) -> list[float]:
    """Rise, fall, rise again -- enough movement to flip HTR in both directions."""
    closes = [base]
    closes += leg(base, base + rise, leg_bars)
    closes += leg(base + rise, base + rise - fall, leg_bars)
    closes += leg(base + rise - fall, base + rise - fall + rise, leg_bars)
    return closes
