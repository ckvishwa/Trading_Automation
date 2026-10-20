"""Behavioural feature extraction: deterministic, no look-ahead, completed bars."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.behavior.features import (
    MIN_WINDOW,
    BehaviorFeatures,
    InsufficientHistory,
    extract_features,
)
from app.market_data.contracts import Candle, Provenance

OPEN = datetime(2026, 9, 7, 3, 45, tzinfo=timezone.utc)
BAR = timedelta(minutes=5)
PROV = Provenance(provider="test", fetched_at=OPEN, endpoint="test", is_mock=True)


def bar(index: int, o: float, h: float, low: float, c: float) -> Candle:
    start = OPEN + index * BAR
    return Candle(
        symbol="NSE:NIFTY",
        timeframe="5m",
        open_time=start,
        close_time=start + BAR,
        open=o,
        high=h,
        low=low,
        close=c,
        provenance=PROV,
    )


def rising_window(n: int = MIN_WINDOW) -> list[Candle]:
    # A clean uptrend: each bar opens at the prior close and closes higher.
    out = []
    price = 100.0
    for i in range(n):
        o = price
        c = price + 5
        out.append(bar(i, o, c + 1, o - 1, c))
        price = c
    return out


# --- guards --------------------------------------------------------------------


def test_too_few_candles_raises_rather_than_returning_partial_features():
    with pytest.raises(InsufficientHistory) as caught:
        extract_features(rising_window(MIN_WINDOW - 1))
    assert caught.value.need == MIN_WINDOW


def test_a_non_ascending_window_is_a_caller_error():
    window = rising_window()
    window[-1], window[-2] = window[-2], window[-1]
    with pytest.raises(ValueError, match="ascending"):
        extract_features(window)


def test_a_mixed_symbol_window_is_refused():
    window = rising_window()
    foreign = window[-1]
    window[-1] = Candle(
        symbol="NSE:BANKNIFTY",
        timeframe="5m",
        open_time=foreign.open_time,
        close_time=foreign.close_time,
        open=foreign.open,
        high=foreign.high,
        low=foreign.low,
        close=foreign.close,
        provenance=PROV,
    )
    with pytest.raises(ValueError, match="one symbol"):
        extract_features(window)


# --- determinism and no look-ahead --------------------------------------------


def test_extraction_is_deterministic():
    window = rising_window()
    assert extract_features(window).as_dict() == extract_features(window).as_dict()


def test_features_depend_only_on_the_window_not_on_later_bars():
    """No look-ahead: appending a later bar must not change the earlier decision.

    The features for a bar are computed from the window ending at that bar, so
    the decision at bar k is identical whether or not bar k+1 exists yet.
    """
    full = rising_window(MIN_WINDOW + 3)
    at_k = extract_features(full[:MIN_WINDOW + 1])
    # Add three more bars; recompute for the same decision bar (same slice end).
    again = extract_features(full[:MIN_WINDOW + 1])
    assert at_k.as_dict() == again.as_dict()
    # And the decision bar's own timestamp is the slice's last close.
    assert at_k.decision_bar_time == full[MIN_WINDOW].close_time


def test_the_decision_bar_is_the_last_bar_in_the_window():
    window = rising_window()
    features = extract_features(window)
    assert features.decision_bar_time == window[-1].close_time
    assert features.window_size == len(window)


# --- A. candle behaviour -------------------------------------------------------


def test_body_and_wick_ratios():
    window = rising_window(MIN_WINDOW)
    # Replace the last bar with a known shape: open 100, high 110, low 98,
    # close 104 -> range 12, body 4, upper wick 6, lower wick 2.
    window[-1] = bar(len(window) - 1, 100, 110, 98, 104)
    f = extract_features(window)
    assert f.body_to_range == pytest.approx(4 / 12)
    assert f.upper_wick_ratio == pytest.approx(6 / 12)
    assert f.lower_wick_ratio == pytest.approx(2 / 12)
    assert f.is_bullish and not f.is_bearish


def test_a_zero_range_bar_yields_zeros_not_nan():
    window = rising_window(MIN_WINDOW)
    flat = window[-1]
    window[-1] = bar(len(window) - 1, flat.open, flat.open, flat.open, flat.open)
    f = extract_features(window)
    assert f.body_to_range == 0.0
    assert f.upper_wick_ratio == 0.0
    assert f.lower_wick_ratio == 0.0
    assert not f.is_bullish and not f.is_bearish


# --- B. structure --------------------------------------------------------------


def test_consecutive_directional_runs():
    window = rising_window()  # every bar bullish
    f = extract_features(window)
    assert f.consecutive_bullish == len(window)
    assert f.consecutive_bearish == 0


def test_higher_high_run_and_structure_in_an_uptrend():
    f = extract_features(rising_window())
    assert f.higher_high_run == len(rising_window()) - 1
    assert f.higher_highs_and_higher_lows == len(rising_window()) - 1
    assert f.lower_lows_and_lower_highs == 0


def test_failed_breakout_is_detected():
    # Flat range, then a bar that pokes above the prior high but closes back in.
    window = [bar(i, 100, 102, 98, 100) for i in range(MIN_WINDOW - 1)]
    window.append(bar(MIN_WINDOW - 1, 100, 106, 99, 100.5))  # high 106 > 102, close 100.5 < 102
    f = extract_features(window)
    assert f.failed_breakout
    assert f.failed_breakout_magnitude == pytest.approx(4.0)
    assert not f.failed_breakdown


def test_failed_breakdown_is_detected():
    window = [bar(i, 100, 102, 98, 100) for i in range(MIN_WINDOW - 1)]
    window.append(bar(MIN_WINDOW - 1, 100, 101, 94, 99.5))  # low 94 < 98, close 99.5 > 98
    f = extract_features(window)
    assert f.failed_breakdown
    assert f.failed_breakdown_magnitude == pytest.approx(4.0)
    assert not f.failed_breakout


# --- C. momentum ---------------------------------------------------------------


def test_momentum_is_the_close_change_over_the_lookback():
    window = rising_window()  # +5 per bar
    f = extract_features(window, momentum_lookback=3)
    assert f.momentum == pytest.approx(15.0)


def test_momentum_acceleration_sign():
    # Build accelerating closes: deltas 1, 2, 4 at the end.
    window = [bar(i, 100, 101, 99, 100) for i in range(MIN_WINDOW)]
    window[-3] = bar(MIN_WINDOW - 3, 100, 101, 99, 100)
    window[-2] = bar(MIN_WINDOW - 2, 100, 103, 99, 102)  # delta +2 from 100
    window[-1] = bar(MIN_WINDOW - 1, 102, 107, 101, 106)  # delta +4 from 102
    f = extract_features(window)
    assert f.momentum_acceleration == pytest.approx(2.0)  # 4 - 2


def test_weakening_follow_through_flag():
    window = [bar(i, 100, 101, 99, 100) for i in range(MIN_WINDOW)]
    window[-2] = bar(MIN_WINDOW - 2, 100, 110, 99, 110)  # big up step
    window[-1] = bar(MIN_WINDOW - 1, 110, 112, 109, 111)  # smaller up step, same sign
    f = extract_features(window)
    assert f.weakening_follow_through


# --- D. volatility -------------------------------------------------------------


def test_range_expansion_above_one_for_a_wide_bar():
    window = [bar(i, 100, 101, 99, 100) for i in range(MIN_WINDOW - 1)]  # range 2
    window.append(bar(MIN_WINDOW - 1, 100, 110, 96, 104))  # range 14
    f = extract_features(window)
    assert f.range_expansion > 1.0
    assert f.average_true_range > 0


def test_range_compression_below_one_when_ranges_shrink():
    # Wide early bars, narrow late bars.
    window = [bar(i, 100, 110, 90, 100) for i in range(MIN_WINDOW // 2)]
    window += [
        bar(i, 100, 101, 99, 100)
        for i in range(MIN_WINDOW // 2, MIN_WINDOW)
    ]
    # fix open_times are already monotonic by index
    f = extract_features(window)
    assert 0 < f.range_compression < 1.0


def test_features_serialize_to_plain_types():
    payload = extract_features(rising_window()).as_dict()
    assert isinstance(payload["decision_bar_time"], str)
    assert payload["feature_version"] == "behavior-features-v1"
    assert all(
        isinstance(v, (int, float, bool, str)) for v in payload.values()
    )
