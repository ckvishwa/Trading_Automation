"""The M1 parity gate refuses to pass on synthetic input."""

from __future__ import annotations

import json
from datetime import timezone

import pytest

from app.research.htr_parity import (
    RecordedAlert,
    SyntheticEvidenceRejected,
    load_recorded_alerts,
    run_parity,
)
from app.research.htr_reference import compute
from tests.synthetic import candles_from_closes, zigzag_closes

CLOSES = zigzag_closes()


def reference_alerts(source: str) -> tuple[RecordedAlert, ...]:
    """The reference's own signals, relabelled with a given provenance."""
    bars = candles_from_closes(CLOSES)
    return tuple(
        RecordedAlert(
            bar_close_time=signal.bar_close_time,
            signal=signal.signal,
            source=source,
            price=signal.price,
        )
        for signal in compute(bars).signals
    )


def test_genuine_matching_alerts_reach_parity():
    bars = candles_from_closes(CLOSES)
    result = run_parity(bars, reference_alerts("tradingview"))
    assert result.genuine
    assert result.parity
    assert result.alert_count == result.reference_signal_count


def test_synthetic_input_can_never_reach_parity():
    """Even perfectly matching synthetic alerts do not close the gate.

    The numbers match -- they are the reference's own output -- but the
    provenance is not TradingView, so parity is withheld. This is the guardrail
    the task asks for: no PASS from fixtures.
    """
    bars = candles_from_closes(CLOSES)
    result = run_parity(bars, reference_alerts("synthetic"))
    assert not result.genuine
    assert not result.parity
    # The underlying numeric comparison still matched; only genuineness blocks it.
    assert result.report["parity"] is True


def test_a_direction_mismatch_fails_even_when_genuine():
    bars = candles_from_closes(CLOSES)
    alerts = reference_alerts("tradingview")
    flipped = (
        RecordedAlert(
            bar_close_time=alerts[0].bar_close_time,
            signal="BUY" if alerts[0].signal == "SELL" else "SELL",
            source="tradingview",
            price=alerts[0].price,
        ),
    ) + alerts[1:]
    result = run_parity(bars, flipped)
    assert result.genuine
    assert not result.parity
    assert result.report["direction_mismatches"]


def test_an_empty_recording_is_not_parity():
    bars = candles_from_closes(CLOSES)
    result = run_parity(bars, ())
    assert not result.parity
    assert not result.genuine


def test_loading_rejects_a_non_genuine_source(tmp_path):
    path = tmp_path / "alerts.jsonl"
    path.write_text(
        json.dumps(
            {"bar_close_time": "2026-10-19T04:20:00Z", "signal": "SELL", "source": "fixture"}
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SyntheticEvidenceRejected, match="not genuine"):
        load_recorded_alerts(path)


def test_loading_accepts_genuine_records(tmp_path):
    path = tmp_path / "alerts.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"bar_close_time": "2026-10-19T04:20:00Z", "signal": "SELL", "source": "tradingview", "price": 25010.5},
                {"bar_close_time": "2026-10-19T05:25:00+00:00", "signal": "BUY", "source": "tradingview"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    alerts = load_recorded_alerts(path)
    assert len(alerts) == 2
    assert alerts[0].bar_close_time.tzinfo == timezone.utc
    assert alerts[0].signal == "SELL"
    assert all(alert.is_genuine for alert in alerts)


def test_loading_rejects_a_bad_direction(tmp_path):
    path = tmp_path / "alerts.jsonl"
    path.write_text(
        json.dumps(
            {"bar_close_time": "2026-10-19T04:20:00Z", "signal": "SIDEWAYS", "source": "tradingview"}
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="BUY or SELL"):
        load_recorded_alerts(path)


@pytest.mark.live
def test_parity_against_real_tradingview_alerts_is_not_yet_possible():
    """The real gate. Skipped until a genuine recording exists.

    Supply genuine alerts as JSON lines with source="tradingview" at
    data/reference/htr_genuine_alerts.jsonl, plus the candles for the same
    window, and this becomes the live M1 parity check.
    """
    from app.config import PROJECT_ROOT

    path = PROJECT_ROOT / "data" / "reference" / "htr_genuine_alerts.jsonl"
    if not path.exists():
        pytest.skip(
            "No genuine TradingView HTR alerts recorded yet. Capture them via the "
            "webhook (Bullish/Bearish Trend Alert, Once Per Bar Close) and save as "
            f"{path}. See docs/M1_HTR_EVIDENCE.md."
        )
    load_recorded_alerts(path)  # would run the real comparison once data exists
