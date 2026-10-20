"""Deterministic behavioural features over completed candles.

These describe *what the price did* on and before the bar an HTR signal fired.
They generate no signal of their own: the HTR indicator remains the sole signal
generator, and nothing here decides to buy or sell. The decision engine (see
`decision_engine.py`) reads these features to ACCEPT or REJECT an HTR signal.

Three invariants, all load-bearing:

* **Completed candles only.** The inputs are `Candle` objects, which do not
  exist for a partial bar (see `app/market_data/contracts.py`).
* **No look-ahead.** Every feature is computed from the window ending at the
  decision bar. The window is the last bar plus the bars before it, never a bar
  after it. A caller passing a window that extends past the signal bar is a
  caller bug, not something smoothed over here.
* **Deterministic.** Pure functions of the candle values. The same window always
  yields the same features, bit for bit -- which is what makes a replayed
  decision reproducible.

Nothing here estimates a win rate, an edge, or a probability. Those require
validated historical outcomes the project does not yet have, and manufacturing
them is explicitly out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime

from app.market_data.contracts import Candle

# A feature window needs enough bars for the longest lookback below (momentum
# acceleration reads three closes; the volatility ratio splits the window). Set
# high enough that every feature is well defined, low enough to be reachable
# early in a session.
MIN_WINDOW = 10


class InsufficientHistory(ValueError):
    """Fewer completed candles than the features need. Never papered over."""

    def __init__(self, have: int, need: int) -> None:
        super().__init__(f"need at least {need} completed candles, have {have}")
        self.have = have
        self.need = need


def _true_range(current: Candle, previous: Candle | None) -> float:
    if previous is None:
        return current.high - current.low
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


@dataclass(frozen=True)
class BehaviorFeatures:
    """A frozen, serialisable snapshot of price behaviour at the decision bar.

    Field names are the behaviours in the spec. Ratios are dimensionless;
    momentum and displacement are in price units; counts are bar counts. A
    feature that is undefined for a bar (an upper wick on a zero-range doji) is
    0.0, never `nan`, so downstream comparisons are total.
    """

    decision_bar_time: datetime
    window_size: int
    feature_version: str = "behavior-features-v1"

    # A. candle behaviour (on the decision bar)
    body_to_range: float = 0.0
    upper_wick_ratio: float = 0.0
    lower_wick_ratio: float = 0.0
    is_bullish: bool = False
    is_bearish: bool = False
    range_expansion: float = 0.0  # decision bar range / mean range of the window

    # B. price structure
    consecutive_bullish: int = 0
    consecutive_bearish: int = 0
    higher_high_run: int = 0
    lower_low_run: int = 0
    higher_highs_and_higher_lows: int = 0
    lower_lows_and_lower_highs: int = 0
    failed_breakout: bool = False
    failed_breakdown: bool = False
    failed_breakout_magnitude: float = 0.0
    failed_breakdown_magnitude: float = 0.0
    repeated_rejection_high: int = 0
    repeated_rejection_low: int = 0

    # C. momentum behaviour
    momentum: float = 0.0  # close[-1] - close[-lookback]
    momentum_acceleration: float = 0.0  # change in the bar-to-bar close delta
    weakening_follow_through: bool = False

    # D. volatility behaviour
    average_true_range: float = 0.0
    range_compression: float = 0.0  # recent mean range / earlier mean range (<1 compressed)
    displacement_vs_atr: float = 0.0  # decision bar body / ATR

    def as_dict(self) -> dict:
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = value.isoformat() if isinstance(value, datetime) else value
        return out


def _candle_behaviour(candle: Candle) -> dict:
    rng = candle.range
    body = candle.body
    upper = candle.high - max(candle.open, candle.close)
    lower = min(candle.open, candle.close) - candle.low
    if rng <= 0:
        return {
            "body_to_range": 0.0,
            "upper_wick_ratio": 0.0,
            "lower_wick_ratio": 0.0,
            "is_bullish": candle.close > candle.open,
            "is_bearish": candle.close < candle.open,
        }
    return {
        "body_to_range": body / rng,
        "upper_wick_ratio": upper / rng,
        "lower_wick_ratio": lower / rng,
        "is_bullish": candle.close > candle.open,
        "is_bearish": candle.close < candle.open,
    }


def _run_lengths(candles: list[Candle]) -> tuple[int, int]:
    bullish = bearish = 0
    for candle in reversed(candles):
        if candle.close > candle.open:
            if bearish:
                break
            bullish += 1
        elif candle.close < candle.open:
            if bullish:
                break
            bearish += 1
        else:
            break
    return bullish, bearish


def _structure(candles: list[Candle]) -> dict:
    higher_high_run = lower_low_run = 0
    for previous, current in zip(candles, candles[1:]):
        higher_high_run = higher_high_run + 1 if current.high > previous.high else 0
        lower_low_run = lower_low_run + 1 if current.low < previous.low else 0
    hh_hl = sum(
        1
        for previous, current in zip(candles, candles[1:])
        if current.high > previous.high and current.low > previous.low
    )
    ll_lh = sum(
        1
        for previous, current in zip(candles, candles[1:])
        if current.low < previous.low and current.high < previous.high
    )
    return {
        "higher_high_run": higher_high_run,
        "lower_low_run": lower_low_run,
        "higher_highs_and_higher_lows": hh_hl,
        "lower_lows_and_lower_highs": ll_lh,
    }


def _failed_moves(candles: list[Candle]) -> dict:
    prior, last = candles[:-1], candles[-1]
    prior_high = max(c.high for c in prior)
    prior_low = min(c.low for c in prior)
    # A breakout that failed: the bar poked above the prior range but closed back
    # inside it. Symmetric for a breakdown.
    failed_breakout = last.high > prior_high and last.close < prior_high
    failed_breakdown = last.low < prior_low and last.close > prior_low
    # How often the window's high/low region was tagged and rejected.
    tolerance = (prior_high - prior_low) * 0.02
    rej_high = sum(1 for c in candles if c.high >= prior_high - tolerance and c.close < c.high)
    rej_low = sum(1 for c in candles if c.low <= prior_low + tolerance and c.close > c.low)
    return {
        "failed_breakout": failed_breakout,
        "failed_breakdown": failed_breakdown,
        "failed_breakout_magnitude": max(0.0, last.high - prior_high) if failed_breakout else 0.0,
        "failed_breakdown_magnitude": max(0.0, prior_low - last.low) if failed_breakdown else 0.0,
        "repeated_rejection_high": rej_high,
        "repeated_rejection_low": rej_low,
    }


def _momentum(candles: list[Candle], lookback: int) -> dict:
    closes = [c.close for c in candles]
    momentum = closes[-1] - closes[-1 - lookback]
    last_delta = closes[-1] - closes[-2]
    prev_delta = closes[-2] - closes[-3]
    acceleration = last_delta - prev_delta
    # Follow-through weakens when the latest step is smaller in magnitude than
    # the one before it while keeping the same sign.
    weakening = (
        last_delta * prev_delta > 0 and abs(last_delta) < abs(prev_delta)
    )
    return {
        "momentum": momentum,
        "momentum_acceleration": acceleration,
        "weakening_follow_through": weakening,
    }


def _volatility(candles: list[Candle]) -> dict:
    ranges = [c.range for c in candles]
    trs = [_true_range(candles[i], candles[i - 1] if i else None) for i in range(len(candles))]
    atr = sum(trs) / len(trs)
    half = len(ranges) // 2
    earlier = ranges[:half]
    recent = ranges[half:]
    earlier_mean = sum(earlier) / len(earlier) if earlier else 0.0
    recent_mean = sum(recent) / len(recent) if recent else 0.0
    compression = (recent_mean / earlier_mean) if earlier_mean > 0 else 0.0
    mean_range = sum(ranges) / len(ranges)
    expansion = (candles[-1].range / mean_range) if mean_range > 0 else 0.0
    displacement = (candles[-1].body / atr) if atr > 0 else 0.0
    return {
        "average_true_range": atr,
        "range_compression": compression,
        "range_expansion": expansion,
        "displacement_vs_atr": displacement,
    }


def extract_features(
    window: list[Candle] | tuple[Candle, ...],
    *,
    momentum_lookback: int = 3,
    min_window: int = MIN_WINDOW,
) -> BehaviorFeatures:
    """Compute behavioural features for the bar at the end of `window`.

    `window` is completed candles in ascending time, the last being the decision
    bar. Raises `InsufficientHistory` rather than returning partial features when
    there are too few bars -- the decision engine turns that into an explicit
    `INSUFFICIENT_HISTORICAL_EVIDENCE` rejection.
    """
    candles = list(window)
    need = max(min_window, momentum_lookback + 1)
    if len(candles) < need:
        raise InsufficientHistory(len(candles), need)

    previous: Candle | None = None
    bar_duration = candles[0].close_time - candles[0].open_time
    for candle in candles:
        if previous is not None and candle.open_time <= previous.open_time:
            raise ValueError("feature window must be strictly ascending by open_time")
        if candle.symbol != candles[0].symbol or candle.timeframe != candles[0].timeframe:
            raise ValueError("feature window must be one symbol and one timeframe")
        if candle.close_time - candle.open_time != bar_duration:
            raise ValueError("feature window must use one bar duration")
        previous = candle

    if any(current.open_time != earlier.close_time for earlier, current in zip(candles, candles[1:])):
        raise ValueError("feature window must contain contiguous completed bars")

    last = candles[-1]
    data: dict = {}
    data.update(_candle_behaviour(last))
    bullish_run, bearish_run = _run_lengths(candles)
    data["consecutive_bullish"] = bullish_run
    data["consecutive_bearish"] = bearish_run
    data.update(_structure(candles))
    data.update(_failed_moves(candles))
    data.update(_momentum(candles, momentum_lookback))
    data.update(_volatility(candles))

    return BehaviorFeatures(
        decision_bar_time=last.close_time,
        window_size=len(candles),
        **data,
    )


# `field` is imported for frozen dataclasses that may gain mutable defaults.
_ = field
