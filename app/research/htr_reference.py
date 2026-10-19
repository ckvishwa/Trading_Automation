"""Independent Python reference for Half Trend Regression [AlgoAlpha].

Transcribed statement by statement from the genuine Pine v5 source held at
`pine/half_trend_regression_algoalpha.pine` (© AlgoAlpha, Mozilla Public
License 2.0). Its purpose is offline parity testing and historical signal
reproduction for backtesting -- **not** to replace TradingView as the live
signal generator. The live architecture is unchanged: TradingView fires the
alert, the webhook records it.

What the source actually says, and what follows from it:

* The signal is a sign change of `trendDirection`, which only ever holds `na`,
  `1` or `-1`:
      BUY  = `ta.crossover(trendDirection, 0)`   (-1 -> 1)
      SELL = `ta.crossunder(trendDirection, 0)`  (1 -> -1)
  A transition out of `na` produces **no** signal, because a comparison against
  `na` is false. The indicator's own initialization is therefore silent.
* The direction gates read `highMA`, `maxLow`, `closes`, `lows[1]`, `lowMA`,
  `minHigh` and `highs[1]` only. `channelDeviation`, `halfATR`, `channelOffset`,
  `upperChannel`, `lowerChannel` and `trendLine` feed plots, fills and label
  placement exclusively. **ATR and Channel Deviation cannot change a signal**;
  `test_channel_deviation_cannot_change_a_signal` pins that down.
* `maxLow` and `minHigh` are `var` state with no periodic reset. `maxLow` is
  seeded with the **first loaded bar's low** and keeps ratcheting up until the
  first flip to -1 reseeds it; `minHigh` is seeded with the first bar's high and
  is immediately poisoned to `na` by `math.min(minHigh, highestHigh)` while
  `highestHigh` is still `na`. So the number of bars a chart has loaded can move
  the **first** flip; once a run has flipped, both pieces of state are reseeded
  from that bar and runs over the same tail converge. Signals after both runs
  have flipped once agree exactly. This is a property of the indicator, not of
  this transcription, and it is why a parity comparison must start the Python
  series where the chart's history starts.
* `ta.linreg(..., 0)` reads the current bar, so values move while a bar is still
  forming and `trendDirection` can flip and unflip intrabar. Pine rolls `var`
  state back to the last bar close on each realtime recalculation, so a closed
  bar's state is committed: historical bars do not repaint, the forming bar
  does. Hence `Once Per Bar Close` is mandatory on the alert, and the engine
  independently rejects any alert that fired before its bar closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from app.market_data.contracts import Candle, CandleSeries
from app.research.pine_ta import (
    Maybe,
    gt,
    highest,
    linreg_series,
    lowest,
    lt,
    na_max,
    na_min,
    sma,
)

SOURCE_PATH = "pine/half_trend_regression_algoalpha.pine"
INDICATOR_NAME = "Half Trend Regression [AlgoAlpha]"

BUY = "BUY"
SELL = "SELL"


@dataclass(frozen=True)
class HTRSettings:
    """Indicator inputs, with the defaults declared in the Pine source.

    `channel_deviation` is accepted because the indicator declares it, and is
    then unused: it belongs to the channel drawing, not to the trend logic.
    `atr_period` is hardcoded to 100 in the source and is likewise display-only.
    """

    amplitude: int = 2
    channel_deviation: float = 2.0
    regression_length: int = 7
    atr_period: int = 100


@dataclass(frozen=True)
class HTRSignal:
    bar_index: int
    bar_close_time: datetime
    signal: str
    price: float


@dataclass(frozen=True)
class HTRBarState:
    """Per-bar internals, kept so a parity mismatch can be diagnosed."""

    bar_index: int
    close_time: datetime
    trend_direction: int | None
    max_low: Maybe
    min_high: Maybe
    highs: Maybe
    lows: Maybe
    closes: Maybe
    high_ma: Maybe
    low_ma: Maybe
    highest_high: Maybe
    lowest_low: Maybe


@dataclass(frozen=True)
class HTRResult:
    signals: tuple[HTRSignal, ...]
    states: tuple[HTRBarState, ...]

    @property
    def directions(self) -> tuple[int | None, ...]:
        return tuple(state.trend_direction for state in self.states)

    def signal_keys(self) -> tuple[tuple[datetime, str], ...]:
        """Comparable identity of each signal: bar close time and direction."""
        return tuple((signal.bar_close_time, signal.signal) for signal in self.signals)


def compute(
    candles: Sequence[Candle] | CandleSeries, settings: HTRSettings | None = None
) -> HTRResult:
    """Replay the indicator over completed candles, oldest first.

    Only completed candles are accepted, matching the `Once Per Bar Close` alert
    the engine requires. Bar `i` reads nothing above index `i`.
    """
    settings = settings or HTRSettings()
    bars: list[Candle] = list(candles)
    if not bars:
        return HTRResult(signals=(), states=())

    amplitude = settings.amplitude
    linreglen = settings.regression_length

    highs_raw = [bar.high for bar in bars]
    lows_raw = [bar.low for bar in bars]
    closes_raw = [bar.close for bar in bars]

    # highs = ta.linreg(high, linreglen, 0), and likewise for low and close.
    highs = linreg_series(highs_raw, linreglen, 0)
    lows = linreg_series(lows_raw, linreglen, 0)
    closes = linreg_series(closes_raw, linreglen, 0)

    # var float trendLine = na / var int trendDirection = na
    # var float maxLow = low  / var float minHigh = high   (first bar's values)
    trend_line: Maybe = None
    trend_direction: int | None = None
    max_low: Maybe = lows_raw[0]
    min_high: Maybe = highs_raw[0]

    signals: list[HTRSignal] = []
    states: list[HTRBarState] = []

    for index, bar in enumerate(bars):
        high_ma = sma(highs, index, amplitude)
        low_ma = sma(lows, index, amplitude)
        highest_high = highest(highs, index, amplitude)
        lowest_low = lowest(lows, index, amplitude)
        previous_direction = trend_direction

        if trend_direction is None and index > linreglen:
            trend_direction = 1
            trend_line = lowest_low
        else:
            if trend_direction == 1:
                max_low = na_max(max_low, lowest_low)
                if lt(high_ma, max_low) and lt(closes[index], lows[index - 1] if index else None):
                    trend_direction = -1
                    trend_line = highest_high
                    min_high = highs[index]
            else:
                # Reached while trendDirection is na as well: `na == 1` is false
                # in Pine, so warm-up bars run this branch.
                min_high = na_min(min_high, highest_high)
                if gt(low_ma, min_high) and gt(
                    closes[index], highs[index - 1] if index else None
                ):
                    trend_direction = 1
                    trend_line = lowest_low
                    max_low = lows[index]

        if trend_direction == 1:
            trend_line = na_max(trend_line, lowest_low)
        else:
            trend_line = na_min(trend_line, highest_high)

        # ta.crossover/crossunder against 0. Both are false when the previous
        # value is na, so leaving na is silent.
        if previous_direction is not None and trend_direction is not None:
            if previous_direction <= 0 < trend_direction:
                signals.append(
                    HTRSignal(
                        bar_index=index,
                        bar_close_time=bar.close_time,
                        signal=BUY,
                        price=bar.close,
                    )
                )
            elif previous_direction >= 0 > trend_direction:
                signals.append(
                    HTRSignal(
                        bar_index=index,
                        bar_close_time=bar.close_time,
                        signal=SELL,
                        price=bar.close,
                    )
                )

        states.append(
            HTRBarState(
                bar_index=index,
                close_time=bar.close_time,
                trend_direction=trend_direction,
                max_low=max_low,
                min_high=min_high,
                highs=highs[index],
                lows=lows[index],
                closes=closes[index],
                high_ma=high_ma,
                low_ma=low_ma,
                highest_high=highest_high,
                lowest_low=lowest_low,
            )
        )

    del trend_line  # computed for fidelity to the source; display-only
    return HTRResult(signals=tuple(signals), states=tuple(states))


def compare_with_recorded(
    computed: Sequence[HTRSignal], recorded: Sequence[HTRSignal]
) -> dict:
    """Bar-for-bar parity report between this reference and genuine alerts.

    Parity is exact or it is a failure: there is no tolerance band, because a
    single disagreement means the backtest and the live path are running
    different indicators.
    """
    computed_by_time = {signal.bar_close_time: signal for signal in computed}
    recorded_by_time = {signal.bar_close_time: signal for signal in recorded}
    all_times = sorted(set(computed_by_time) | set(recorded_by_time))

    matched: list[datetime] = []
    direction_mismatches: list[dict] = []
    missing_in_python: list[datetime] = []
    extra_in_python: list[datetime] = []

    for moment in all_times:
        mine = computed_by_time.get(moment)
        theirs = recorded_by_time.get(moment)
        if mine is None:
            missing_in_python.append(moment)
        elif theirs is None:
            extra_in_python.append(moment)
        elif mine.signal != theirs.signal:
            direction_mismatches.append(
                {"bar_close_time": moment, "python": mine.signal, "tradingview": theirs.signal}
            )
        else:
            matched.append(moment)

    return {
        "compared": len(all_times),
        "matched": len(matched),
        "direction_mismatches": direction_mismatches,
        "missing_in_python": missing_in_python,
        "extra_in_python": extra_in_python,
        "parity": bool(all_times)
        and not direction_mismatches
        and not missing_in_python
        and not extra_in_python,
    }
