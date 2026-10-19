import pytest

from app.research.pine_ta import (
    atr,
    gt,
    highest,
    linreg,
    linreg_series,
    lowest,
    lt,
    na_max,
    na_min,
    rma,
    sma,
    true_range,
)


def test_linreg_of_a_perfectly_linear_series_is_the_current_value():
    # ta.linreg(src, n, 0) returns the fitted value AT the current bar, so a
    # straight line fits itself exactly.
    series = [10.0 + 3.0 * i for i in range(10)]
    assert linreg(series, 9, 7, 0) == pytest.approx(series[9])
    assert linreg(series, 6, 7, 0) == pytest.approx(series[6])


def test_linreg_offset_steps_back_along_the_fitted_line():
    series = [10.0 + 3.0 * i for i in range(10)]
    assert linreg(series, 9, 7, 1) == pytest.approx(series[9] - 3.0)
    assert linreg(series, 9, 7, 2) == pytest.approx(series[9] - 6.0)


def test_linreg_smooths_a_noisy_series_towards_its_trend():
    # A single spike must not drag the fitted value all the way to the spike.
    series = [100.0] * 6 + [160.0]
    value = linreg(series, 6, 7, 0)
    assert 100.0 < value < 160.0


def test_linreg_is_na_until_the_window_is_full():
    series = [1.0, 2.0, 3.0]
    assert linreg(series, 1, 3, 0) is None
    assert linreg(series, 2, 3, 0) is not None


def test_linreg_series_marks_the_warm_up_as_na():
    values = linreg_series([float(i) for i in range(10)], 7, 0)
    assert values[:6] == [None] * 6
    assert all(value is not None for value in values[6:])


def test_linreg_of_a_flat_series_is_the_flat_value():
    assert linreg([5.0] * 7, 6, 7, 0) == pytest.approx(5.0)


@pytest.mark.parametrize("function", [sma, highest, lowest])
def test_window_functions_are_na_until_the_window_is_full(function):
    series = [1.0, 2.0, 3.0]
    assert function(series, 0, 2) is None
    assert function(series, 1, 2) is not None


@pytest.mark.parametrize("function", [sma, highest, lowest])
def test_window_functions_are_na_if_any_input_is_na(function):
    assert function([None, 2.0, 3.0], 2, 3) is None


def test_window_function_values():
    series = [1.0, 5.0, 3.0]
    assert sma(series, 2, 3) == pytest.approx(3.0)
    assert highest(series, 2, 3) == 5.0
    assert lowest(series, 2, 3) == 1.0


def test_zero_or_negative_length_is_a_programming_error():
    with pytest.raises(ValueError):
        sma([1.0], 0, 0)


def test_na_propagates_through_max_and_min():
    # Pine: math.max(na, 5) is na, NOT 5. HTR depends on this: it is what
    # poisons minHigh during warm-up.
    assert na_max(None, 5.0) is None
    assert na_max(5.0, None) is None
    assert na_min(None, 5.0) is None
    assert na_max(4.0, 5.0) == 5.0
    assert na_min(4.0, 5.0) == 4.0


def test_comparisons_involving_na_are_false_in_both_directions():
    assert lt(None, 5.0) is False
    assert gt(None, 5.0) is False
    assert lt(5.0, None) is False
    assert gt(5.0, None) is False
    assert lt(4.0, 5.0) is True
    assert gt(5.0, 4.0) is True


def test_true_range_uses_high_minus_low_on_the_first_bar():
    highs, lows, closes = [10.0, 12.0], [8.0, 11.0], [9.0, 11.5]
    ranges = true_range(highs, lows, closes)
    assert ranges[0] == pytest.approx(2.0)
    # max(12-11, |12-9|, |11-9|) == 3
    assert ranges[1] == pytest.approx(3.0)


def test_rma_seeds_with_a_simple_average_then_smooths():
    values = rma([2.0, 4.0, 6.0, 8.0], 2)
    assert values[0] is None
    assert values[1] == pytest.approx(3.0)
    assert values[2] == pytest.approx((3.0 + 6.0) / 2)


def test_atr_is_available_only_once_the_period_is_covered():
    highs = [float(10 + i) for i in range(5)]
    lows = [float(8 + i) for i in range(5)]
    closes = [float(9 + i) for i in range(5)]
    values = atr(highs, lows, closes, 3)
    assert values[:2] == [None, None]
    assert values[2] is not None
