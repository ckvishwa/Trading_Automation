"""The handful of Pine Script built-ins Half Trend Regression depends on.

Written to Pine v5 semantics, not to "whatever NumPy does", because the whole
point of the reference implementation is to agree with TradingView bar for bar.
Three Pine behaviours are reproduced deliberately:

* **Insufficient history yields `na`.** `ta.linreg(src, 7, 0)` has no value until
  seven bars exist. Returning a value computed from fewer bars would make the
  Python reference emit signals TradingView never emitted.
* **`na` propagates through `math.max`/`math.min`.** `math.max(na, 5)` is `na` in
  Pine, not `5`. HTR's `maxLow := math.max(maxLow, lowestLow)` therefore poisons
  its own state while `lowestLow` is still `na`, and that is load-bearing for how
  the indicator warms up. See `app/research/htr_reference.py`.
* **Any comparison involving `na` is false.** `na < 5` is false and `na > 5` is
  also false, so a gate like `highMA < maxLow` simply does not fire during
  warm-up instead of raising or defaulting.

`None` represents `na` throughout. Series are plain lists indexed by bar, so a
bar's calculation can only see indices at or below its own -- the structure makes
look-ahead hard to write by accident.
"""

from __future__ import annotations

from typing import Sequence

Maybe = float | None


def na_max(left: Maybe, right: Maybe) -> Maybe:
    """Pine `math.max`: na in, na out."""
    if left is None or right is None:
        return None
    return max(left, right)


def na_min(left: Maybe, right: Maybe) -> Maybe:
    """Pine `math.min`: na in, na out."""
    if left is None or right is None:
        return None
    return min(left, right)


def lt(left: Maybe, right: Maybe) -> bool:
    """Pine `<`: false whenever either side is na."""
    return left is not None and right is not None and left < right


def gt(left: Maybe, right: Maybe) -> bool:
    """Pine `>`: false whenever either side is na."""
    return left is not None and right is not None and left > right


def _window(series: Sequence[Maybe], index: int, length: int) -> list[Maybe] | None:
    if length <= 0:
        raise ValueError(f"length must be positive, got {length}")
    start = index - length + 1
    if start < 0:
        return None
    return list(series[start : index + 1])


def sma(series: Sequence[Maybe], index: int, length: int) -> Maybe:
    """Pine `ta.sma`: na until `length` bars exist, and na if any input is na."""
    window = _window(series, index, length)
    if window is None or any(value is None for value in window):
        return None
    return sum(window) / length  # type: ignore[arg-type]


def highest(series: Sequence[Maybe], index: int, length: int) -> Maybe:
    """Pine `ta.highest` over a window; na if the window is short or holds na."""
    window = _window(series, index, length)
    if window is None or any(value is None for value in window):
        return None
    return max(window)  # type: ignore[type-var]


def lowest(series: Sequence[Maybe], index: int, length: int) -> Maybe:
    """Pine `ta.lowest` over a window; na if the window is short or holds na."""
    window = _window(series, index, length)
    if window is None or any(value is None for value in window):
        return None
    return min(window)  # type: ignore[type-var]


def linreg(series: Sequence[Maybe], index: int, length: int, offset: int = 0) -> Maybe:
    """Pine `ta.linreg(source, length, offset)`.

    Fits a least-squares line to the last `length` values, with x = 0 at the
    oldest bar of the window and x = length - 1 at `index`, then returns the
    fitted value at `x = length - 1 - offset`. With `offset = 0` that is the
    fitted value on the current bar -- which is why a perfectly linear input
    returns the input itself.
    """
    window = _window(series, index, length)
    if window is None or any(value is None for value in window):
        return None
    if length == 1:
        return window[0]

    mean_x = (length - 1) / 2
    mean_y = sum(window) / length  # type: ignore[arg-type]
    covariance = 0.0
    variance = 0.0
    for position, value in enumerate(window):
        delta_x = position - mean_x
        covariance += delta_x * (value - mean_y)  # type: ignore[operator]
        variance += delta_x * delta_x
    slope = 0.0 if variance == 0 else covariance / variance
    intercept = mean_y - slope * mean_x
    return intercept + slope * (length - 1 - offset)


def linreg_series(series: Sequence[Maybe], length: int, offset: int = 0) -> list[Maybe]:
    """`linreg` evaluated on every bar, as a Pine series would be."""
    return [linreg(series, index, length, offset) for index in range(len(series))]


def rma(series: Sequence[Maybe], length: int) -> list[Maybe]:
    """Pine `ta.rma`, Wilder's smoothing -- the basis of `ta.atr`.

    Seeded with a simple average of the first `length` values, as Pine does.
    Included for completeness of the channel calculation; HTR's direction logic
    does not use it.
    """
    out: list[Maybe] = [None] * len(series)
    accumulated: float | None = None
    for index, value in enumerate(series):
        if value is None:
            continue
        if accumulated is None:
            window = _window(series, index, length)
            if window is None or any(item is None for item in window):
                continue
            accumulated = sum(window) / length  # type: ignore[arg-type]
        else:
            accumulated = (accumulated * (length - 1) + value) / length
        out[index] = accumulated
    return out


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[Maybe]:
    """Pine's true range: `high - low` on the first bar, then the usual maximum."""
    out: list[Maybe] = []
    for index in range(len(highs)):
        if index == 0:
            out.append(highs[0] - lows[0])
            continue
        previous_close = closes[index - 1]
        out.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - previous_close),
                abs(lows[index] - previous_close),
            )
        )
    return out


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], length: int
) -> list[Maybe]:
    """Pine `ta.atr(length)` = `ta.rma(ta.tr(true), length)`."""
    return rma(true_range(highs, lows, closes), length)
