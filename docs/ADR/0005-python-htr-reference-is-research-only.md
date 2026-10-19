# ADR 0005 — The Python HTR reference is research-only, and the bar grid is anchored on the session open

* Status: Accepted
* Date: 2026-10-19
* Milestone: M1

## Context

The genuine Pine v5 source of Half Trend Regression [AlgoAlpha] is now held at
`pine/half_trend_regression_algoalpha.pine`. That makes two things possible that
were previously blocked: reproducing historical signals for backtesting, and
checking the live signal path against an independent implementation.

It also creates a risk. Once a Python implementation of the indicator exists, it
is tempting to let it generate signals directly and drop the TradingView round
trip. That would change the agreed architecture, and it would mean the backtest
and the live system are the same code — so a transcription error would be
invisible in testing and present in production.

## Decision

1. **TradingView remains the only signal generator.** `app/research/` is not
   imported by `app/api/`, `app/signals/` or (when they exist) `app/behavior/`,
   `app/strategy/` or `app/execution/`. The reference exists for parity testing
   and historical replay.
2. **Transcribe, do not improve.** `htr_reference.py` follows the source
   statement by statement, including behaviour that looks like a defect: `na`
   propagation through `math.max`/`math.min`, the silent warm-up, and `maxLow`
   being seeded from the first loaded bar's low. `pine_ta.py` reproduces Pine's
   `na` semantics for the same reason. Tests pin these quirks; a "cleaner"
   implementation that smoothed them away would disagree with TradingView.
3. **Parity is exact or it is failure.** `compare_with_recorded` reports parity
   only when every signal matches on both bar-close time and direction, and an
   empty comparison never reads as success.
4. **Intraday bar grids are anchored on the IST session open**, taken from
   `data/reference/nse_calendar.json`, in three places that must agree: the
   signal contract's alignment check (`BAR_NOT_ALIGNED_TO_TIMEFRAME`), the candle
   builder's bucketing, and the reference's input series. Anchoring on UTC
   midnight would be wrong for any timeframe that does not divide the 09:15 IST
   offset evenly — a 10-minute NSE bar opens at 09:15, 09:25, which is not on a
   UTC 10-minute grid.
5. **The alert must be `Once Per Bar Close`, and the engine does not trust that
   it is.** The source recomputes `ta.linreg(..., 0)` on the forming bar, so
   `trendDirection` can flip and unflip intrabar. An alert whose bar had not
   closed when it fired is rejected (`BAR_CLOSE_AFTER_FIRE_TIME`).

## Alternatives considered

* **Generate signals in Python and drop TradingView.** Rejected: it discards the
  independent check, and makes any transcription error undetectable.
* **Approximate the indicator for speed.** Rejected: an approximation that
  disagrees on a handful of bars produces a backtest of a different strategy.
* **Anchor bars on UTC midnight.** Rejected as above; it happens to work for
  1/3/5/15-minute bars and silently breaks for others.

## Consequences

* Signals cannot be reproduced for a period with no candle history, so M7's
  historical coverage is bounded by the research provider's depth (Upstox
  intraday from January 2022; FYERS per-request caps).
* A parity run must start the Python series where the chart's history starts,
  because the **first** flip depends on loaded history. Signals after both runs
  have flipped agree, which is what makes long backtests usable.
* The M1 gate still needs recorded genuine alerts. The placeholder test is
  marked `live` and skips with a message naming exactly what to supply, so the
  gap is visible in every test run rather than buried in a document.
