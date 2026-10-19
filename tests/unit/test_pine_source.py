"""Guards on the Pine files.

The saved indicator is the project's signal definition. If it is edited, the
Python reference and every parity claim built on it silently stop describing the
live signal. These tests make such an edit fail loudly, and keep the optional
adapter copy honest about being logic-identical to the original.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.research.htr_reference import HTRSettings

PINE_DIR = PROJECT_ROOT / "pine"
ORIGINAL = PINE_DIR / "half_trend_regression_algoalpha.pine"
ADAPTER = PINE_DIR / "htr_alert_adapter.pine"

# From "// Calculations" up to and including the two plotshape lines: the whole
# of the indicator's logic.
LOGIC_START = "// Calculations"
LOGIC_END = 'title="Bullish Trend"'


def logic_block(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == LOGIC_START)
    end = next(index for index, line in enumerate(lines) if LOGIC_END in line)
    return "\n".join(lines[start : end + 1])


@pytest.fixture(scope="module")
def original_text() -> str:
    return ORIGINAL.read_text(encoding="utf-8")


def test_licence_and_attribution_are_intact(original_text):
    assert "Mozilla Public License 2.0" in original_text
    assert "© AlgoAlpha" in original_text
    assert "//@version=5" in original_text
    assert 'indicator("Half Trend Regression [AlgoAlpha]"' in original_text


def test_the_two_direction_alert_conditions_are_unchanged(original_text):
    assert (
        'alertcondition(ta.crossunder(trendDirection, 0), title="Bearish Trend Alert")'
        in original_text
    )
    assert (
        'alertcondition(ta.crossover(trendDirection, 0), title="Bullish Trend Alert")'
        in original_text
    )


def test_universal_alert_exists_in_the_original_but_is_not_used(original_text):
    # It fires on both directions, so a payload from it could not state which.
    assert 'title="Universal Trend Alert"' in original_text
    adapter_calls = [
        line
        for line in ADAPTER.read_text(encoding="utf-8").splitlines()
        if line.startswith("alertcondition(")
    ]
    assert len(adapter_calls) == 2
    assert not any("Universal" in line for line in adapter_calls)


def test_python_reference_defaults_match_the_pine_inputs(original_text):
    settings = HTRSettings()
    assert f'input.int({settings.amplitude}, "Amplitude"' in original_text
    assert f'input.float({settings.channel_deviation}, "Channel Deviation"' in original_text
    assert f'input.int({settings.regression_length}, "Linear Regression Length"' in original_text
    assert f"atrPeriod = {settings.atr_period}" in original_text


def test_direction_gates_read_only_the_series_the_reference_uses(original_text):
    assert "if highMA < maxLow and closes < lows[1]" in original_text
    assert "if lowMA > minHigh and closes > highs[1]" in original_text
    assert "if na(trendDirection) and bar_index > linreglen" in original_text


def test_atr_is_referenced_exactly_once_and_only_for_the_channel(original_text):
    # The basis of the "Channel Deviation cannot change a signal" claim.
    assert len(re.findall(r"ta\.atr\(", original_text)) == 1
    assert "halfATR = ta.atr(atrPeriod) / 2" in original_text
    assert "channelOffset = channelDeviation * halfATR" in original_text
    # channelOffset may only reach upperChannel / lowerChannel.
    uses = [
        line
        for line in original_text.splitlines()
        if "channelOffset" in line and not line.strip().startswith("//")
    ]
    assert sorted(line.strip() for line in uses) == [
        "channelOffset = channelDeviation * halfATR",
        "lowerChannel = trendLine - channelOffset",
        "upperChannel = trendLine + channelOffset",
    ]


def test_no_lookahead_or_higher_timeframe_request(original_text):
    for forbidden in ("request.security", "lookahead", "barmerge"):
        assert forbidden not in original_text


def test_adapter_logic_is_identical_to_the_original():
    # The adapter's only purpose is to attach webhook messages. If its logic
    # drifts, it no longer fires on the same bars as the indicator being tested.
    assert logic_block(ADAPTER) == logic_block(ORIGINAL)


def test_adapter_declares_itself_a_modified_copy_under_the_same_licence():
    text = ADAPTER.read_text(encoding="utf-8")
    assert "Mozilla Public License 2.0" in text
    assert "© AlgoAlpha" in text
    assert "MODIFIED COPY" in text


def _message_payload(text: str, variable: str) -> dict:
    match = re.search(rf"^{variable} = '(\{{.*\}})'$", text, flags=re.MULTILINE)
    assert match, f"{variable} not found in the adapter"
    return json.loads(match.group(1))


@pytest.mark.parametrize(
    ("variable", "example"),
    [("buyMessage", "htr-buy-alert.json"), ("sellMessage", "htr-sell-alert.json")],
)
def test_adapter_messages_match_the_alert_templates(variable, example):
    # The alert templates are what the contract tests are written against, so
    # the embedded Pine messages must say exactly the same thing.
    text = ADAPTER.read_text(encoding="utf-8")
    from_pine = _message_payload(text, variable)
    from_file = json.loads((PROJECT_ROOT / "examples" / example).read_text(encoding="utf-8"))
    assert from_pine == from_file


def test_alert_templates_carry_bar_open_time_not_a_close_placeholder():
    # TradingView has no bar-close placeholder: {{time}} is the bar's OPEN, and
    # the field name must keep saying so.
    for example in ("htr-buy-alert.json", "htr-sell-alert.json"):
        payload = json.loads((PROJECT_ROOT / "examples" / example).read_text(encoding="utf-8"))
        assert payload["bar_open_time"] == "{{time}}"
        assert payload["fired_at"] == "{{timenow}}"
        assert payload["mode"] == "PAPER"
        assert payload["event"] == "REVERSAL"
        assert "bar_close_time" not in payload


def test_alert_templates_cover_both_directions_and_nothing_else():
    directions = {
        json.loads((PROJECT_ROOT / "examples" / name).read_text(encoding="utf-8"))["signal"]
        for name in ("htr-buy-alert.json", "htr-sell-alert.json")
    }
    assert directions == {"BUY", "SELL"}
