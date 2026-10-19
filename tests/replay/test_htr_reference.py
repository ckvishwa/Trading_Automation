"""Tests for the Python HTR reference against the genuine Pine v5 source.

These pin the behaviour that was read out of
`pine/half_trend_regression_algoalpha.pine`, including the parts that are
awkward -- warm-up poisoning and first-flip dependence on loaded history. They
are properties of the indicator; the reference is wrong if it hides them.

None of this replaces comparison against real TradingView alerts. See
`test_parity_against_recorded_alerts_is_not_yet_possible`.
"""

from __future__ import annotations

import pytest

from app.research.htr_reference import (
    BUY,
    SELL,
    HTRSettings,
    HTRSignal,
    compare_with_recorded,
    compute,
)
from tests.synthetic import candles_from_closes, zigzag_closes

CLOSES = zigzag_closes()


@pytest.fixture
def bars():
    return candles_from_closes(CLOSES)


def test_defaults_match_the_pine_source():
    settings = HTRSettings()
    assert settings.amplitude == 2
    assert settings.channel_deviation == 2.0
    assert settings.regression_length == 7
    assert settings.atr_period == 100


def test_produces_a_sell_then_a_buy_on_a_rise_fall_rise_path(bars):
    result = compute(bars)
    assert [signal.signal for signal in result.signals] == [SELL, BUY]
    assert [signal.bar_index for signal in result.signals] == [18, 32]


def test_signal_timestamps_are_bar_close_times(bars):
    result = compute(bars)
    for signal in result.signals:
        assert signal.bar_close_time == bars[signal.bar_index].close_time
        # The close time is the bar's exclusive end, matching the webhook
        # contract -- a signal and its candle are never one bar apart.
        assert signal.bar_close_time > bars[signal.bar_index].open_time


def test_trend_direction_only_ever_holds_na_one_or_minus_one(bars):
    assert set(compute(bars).directions) <= {None, 1, -1}


def test_initialization_out_of_na_emits_no_signal(bars):
    # trendDirection goes na -> 1 at bar_index == linreglen + 1 == 8, but
    # ta.crossover against a na previous value is false, so the indicator's own
    # initialization is silent.
    result = compute(bars)
    directions = result.directions
    assert directions[7] is None
    assert directions[8] == 1
    assert all(signal.bar_index != 8 for signal in result.signals)


def test_no_signal_can_occur_during_the_warm_up(bars):
    result = compute(bars)
    assert all(signal.bar_index > HTRSettings().regression_length for signal in result.signals)


def test_min_high_is_poisoned_to_na_during_warm_up(bars):
    # math.min(minHigh, highestHigh) with a na highestHigh yields na in Pine.
    # That is why no flip to +1 can happen before initialization.
    states = compute(bars).states
    assert states[0].min_high is None
    assert all(state.min_high is None for state in states[: states[0].bar_index + 9])


def test_max_low_is_seeded_from_the_first_loaded_bars_low(bars):
    # var float maxLow = low  ->  the first bar of the LOADED history.
    states = compute(bars).states
    assert states[0].max_low == pytest.approx(bars[0].low)


def test_max_low_ratchets_upwards_while_the_trend_is_up(bars):
    states = compute(bars).states
    rising = [state.max_low for state in states[8:16]]
    assert all(
        later >= earlier for earlier, later in zip(rising, rising[1:])
    ), f"maxLow must be monotonic while trendDirection == 1, got {rising}"


def test_channel_deviation_cannot_change_a_signal(bars):
    # channelOffset feeds upperChannel/lowerChannel, which feed plots, fills and
    # label placement only. The direction gates never read them.
    baseline = compute(bars).signal_keys()
    for deviation in (0.1, 0.5, 2.0, 10.0, 50.0):
        assert compute(bars, HTRSettings(channel_deviation=deviation)).signal_keys() == baseline


def test_atr_period_cannot_change_a_signal(bars):
    baseline = compute(bars).signal_keys()
    for period in (5, 100, 500):
        assert compute(bars, HTRSettings(atr_period=period)).signal_keys() == baseline


def test_amplitude_and_regression_length_do_change_signals(bars):
    # Counterpart to the two tests above: the inputs that genuinely feed the
    # direction gates must matter, or those tests would be proving nothing.
    baseline = compute(bars).signal_keys()
    assert compute(bars, HTRSettings(amplitude=8)).signal_keys() != baseline
    assert compute(bars, HTRSettings(regression_length=20)).signal_keys() != baseline


def test_computation_is_deterministic(bars):
    assert compute(bars).signal_keys() == compute(bars).signal_keys()
    assert compute(bars).directions == compute(bars).directions


def test_withholding_later_bars_never_changes_an_earlier_signal(bars):
    """Offline repaint check: a closed bar's signal is final.

    Running the indicator over progressively longer prefixes must never move,
    add or remove a signal that an earlier prefix already produced.
    """
    full = compute(bars).signal_keys()
    for length in range(10, len(bars) + 1):
        prefix = compute(bars[:length]).signal_keys()
        expected = tuple(
            key for key in full if key[0] <= bars[length - 1].close_time
        )
        assert prefix == expected, f"prefix of {length} bars disagreed with the full run"


def test_loaded_history_can_move_the_first_flip(bars):
    """`maxLow` is seeded from the first LOADED bar's low.

    Until the first flip reseeds it, the gate `highMA < maxLow` carries a value
    that depends on how much history the chart has loaded. So the same tail of
    candles can flip on a different bar depending on what preceded it. This is
    the indicator's behaviour, not an artefact of the transcription, and it is
    why a parity comparison must use the same history start as the chart.
    """
    full_first_flip = compute(bars).signals[0].bar_index
    shifted_first_flips = {
        dropped: compute(candles_from_closes(CLOSES[dropped:])).signals[0].bar_index + dropped
        for dropped in (3, 6, 10, 12)
    }
    assert any(
        index != full_first_flip for index in shifted_first_flips.values()
    ), (
        "expected some truncation to move the first flip; if this stops holding, "
        f"the history-dependence note in htr_reference.py is stale ({shifted_first_flips})"
    )


def test_signals_agree_once_both_runs_have_flipped_once(bars):
    """And the dependence does not persist.

    Once a run has flipped, `maxLow`/`minHigh` are reseeded from that bar, so
    two runs over the same tail converge. Signals strictly after both runs' first
    flips must match exactly -- which is what makes backtesting over a long
    history trustworthy despite the warm-up quirk above.
    """
    full = compute(bars)
    full_by_index = {signal.bar_index: signal.signal for signal in full.signals}
    full_first_flip = full.signals[0].bar_index

    for dropped in (3, 6, 10, 12):
        shorter = compute(candles_from_closes(CLOSES[dropped:]))
        shifted = {signal.bar_index + dropped: signal.signal for signal in shorter.signals}
        converged_after = max(full_first_flip, min(shifted))

        assert {
            index: direction for index, direction in shifted.items() if index > converged_after
        } == {
            index: direction
            for index, direction in full_by_index.items()
            if index > converged_after
        }, f"dropping {dropped} leading bars changed a signal after both runs had flipped"


def test_empty_input_produces_no_signals_and_no_states():
    result = compute([])
    assert result.signals == ()
    assert result.states == ()


def test_a_flat_market_produces_no_signals():
    flat = candles_from_closes([24_000.0] * 60)
    assert compute(flat).signals == ()


def test_parity_comparator_reports_exact_agreement(bars):
    computed = compute(bars).signals
    report = compare_with_recorded(computed, computed)
    assert report["parity"] is True
    assert report["matched"] == len(computed)


def test_parity_comparator_flags_a_direction_mismatch(bars):
    computed = compute(bars).signals
    flipped = (
        HTRSignal(
            bar_index=computed[0].bar_index,
            bar_close_time=computed[0].bar_close_time,
            signal=BUY if computed[0].signal == SELL else SELL,
            price=computed[0].price,
        ),
    ) + tuple(computed[1:])
    report = compare_with_recorded(computed, flipped)
    assert report["parity"] is False
    assert len(report["direction_mismatches"]) == 1


def test_parity_comparator_flags_missing_and_extra_signals(bars):
    computed = compute(bars).signals
    report = compare_with_recorded(computed[:1], computed)
    assert report["parity"] is False
    assert report["missing_in_python"] == [computed[1].bar_close_time]

    report = compare_with_recorded(computed, computed[:1])
    assert report["extra_in_python"] == [computed[1].bar_close_time]


def test_parity_of_an_empty_comparison_is_not_reported_as_success():
    # Zero signals compared must never read as parity achieved.
    assert compare_with_recorded((), ())["parity"] is False


@pytest.mark.live
def test_parity_against_recorded_alerts_is_not_yet_possible():
    """Placeholder for the real M1 gate.

    Parity is only meaningful against genuine TradingView alerts: a recorded
    list of (bar_close_time, direction) for a known symbol, timeframe, date
    range and set of indicator inputs, from the owner's chart. No such recording
    exists yet, so this test is marked `live` and skipped by default rather than
    asserting something weaker and calling the gate passed.
    """
    pytest.skip(
        "No recorded genuine TradingView HTR alerts available. Supply them as "
        "(bar_close_time, direction) pairs with the chart's symbol, timeframe "
        "and indicator inputs to close the M1 parity gate."
    )
