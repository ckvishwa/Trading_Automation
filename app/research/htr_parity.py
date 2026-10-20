"""Compare genuine TradingView HTR alerts against the Python reference.

This is the M1 parity gate. It exists to answer one question with evidence: do
the signals the real indicator fired match what `app/research/htr_reference.py`
computes from the same candles? Until that is answered against **genuine**
recorded alerts, historical parity is not PASS -- and this module refuses to
report PASS on synthetic input.

Inputs:

* a recording of genuine alert events -- `(bar_close_time, signal)` pairs that
  the real TradingView alert delivered, each marked with its provenance;
* the completed candles for the same symbol, timeframe and window, from a
  verified market-data source.

It runs the reference over the candles, lines the two up by bar-close time, and
returns the existing exact-or-fail parity report. A record that does not assert
genuine provenance is rejected, so a fixture can never be mistaken for evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.market_data.contracts import Candle, CandleSeries
from app.research.htr_reference import HTRSettings, HTRSignal, compare_with_recorded, compute


class SyntheticEvidenceRejected(ValueError):
    """Raised when alert records are not marked as genuine TradingView output."""


@dataclass(frozen=True)
class RecordedAlert:
    bar_close_time: datetime
    signal: str
    source: str
    price: float | None = None

    @property
    def is_genuine(self) -> bool:
        # "tradingview" provenance only. "synthetic"/"fixture"/"replay" are not
        # evidence of what the real indicator fired.
        return self.source.strip().lower() == "tradingview"


def load_recorded_alerts(path: Path) -> tuple[RecordedAlert, ...]:
    """Load genuine alert records from JSON lines.

    Each line: {"bar_close_time": ISO8601, "signal": "BUY"|"SELL",
    "source": "tradingview", "price": <optional>}. A line whose source is not
    `tradingview` is refused, naming the line, because the whole point is that
    these are real.
    """
    alerts: list[RecordedAlert] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        signal = str(row["signal"]).upper()
        if signal not in ("BUY", "SELL"):
            raise ValueError(f"{path}:{number}: signal must be BUY or SELL, got {signal!r}")
        moment = datetime.fromisoformat(str(row["bar_close_time"]).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        alert = RecordedAlert(
            bar_close_time=moment.astimezone(timezone.utc),
            signal=signal,
            source=str(row.get("source", "")),
            price=float(row["price"]) if row.get("price") is not None else None,
        )
        if not alert.is_genuine:
            raise SyntheticEvidenceRejected(
                f"{path}:{number}: source={alert.source!r} is not genuine "
                "TradingView output; parity evidence must come from real alerts"
            )
        alerts.append(alert)
    return alerts


@dataclass(frozen=True)
class HTRParityResult:
    parity: bool
    report: dict
    alert_count: int
    reference_signal_count: int
    settings: HTRSettings
    genuine: bool

    def as_dict(self) -> dict:
        return {
            "parity": self.parity,
            "genuine_evidence": self.genuine,
            "alert_count": self.alert_count,
            "reference_signal_count": self.reference_signal_count,
            "settings": {
                "amplitude": self.settings.amplitude,
                "channel_deviation": self.settings.channel_deviation,
                "regression_length": self.settings.regression_length,
            },
            "report": self.report,
        }


def run_parity(
    candles: CandleSeries | list[Candle] | tuple[Candle, ...],
    recorded: tuple[RecordedAlert, ...],
    settings: HTRSettings | None = None,
) -> HTRParityResult:
    """Run the reference over candles and compare to the recorded alerts.

    Parity is exact or it is failure, and an empty comparison is never parity --
    both inherited from `compare_with_recorded`. `genuine` is True only if every
    recorded alert asserted TradingView provenance; a result over non-genuine
    input is reported with `parity=False` regardless of the numbers, so synthetic
    data cannot close the gate.
    """
    settings = settings or HTRSettings()
    genuine = bool(recorded) and all(alert.is_genuine for alert in recorded)
    computed = compute(candles, settings).signals
    recorded_signals = tuple(
        HTRSignal(
            bar_index=-1,
            bar_close_time=alert.bar_close_time,
            signal=alert.signal,
            price=alert.price if alert.price is not None else 0.0,
        )
        for alert in recorded
    )
    report = compare_with_recorded(computed, recorded_signals)
    return HTRParityResult(
        parity=bool(report["parity"]) and genuine,
        report=report,
        alert_count=len(recorded),
        reference_signal_count=len(computed),
        settings=settings,
        genuine=genuine,
    )
