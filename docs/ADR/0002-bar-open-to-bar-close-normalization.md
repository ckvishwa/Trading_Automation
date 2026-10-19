# ADR 0002 -- Derive bar close time; never accept it as given

* Status: Accepted
* Date: 2026-10-19
* Milestone: M1/M2

## Context

TradingView's `{{time}}` placeholder renders the **opening** time of the bar that
triggered the alert. The previous Node collector stored that value as
`bar_time` and compared it only against `fired_at`. Every downstream consumer
that reads such a field as "when the signal's bar closed" is then systematically
one bar early -- a silent, direction-independent timestamp error that would
corrupt candle alignment, historical pattern lookups and any outcome measurement
built on them.

Separately, the indicator's repaint behaviour is unverified (see
`pine/README.md`). An alert that fires *before* its bar has closed is describing
an unconfirmed bar, which must not be treated as a confirmed signal.

## Decision

1. The inbound field is named `bar_open_time` and documented as the bar's open.
2. `bar_close_time = bar_open_time + one bar`, computed in
   `app/signals/timeframes.py` and unit-tested. No call site assumes either.
3. Both timestamps are stored. The idempotency key is built from
   `bar_close_time`, so it identifies the bar event the signal belongs to.
4. Daily and higher timeframes are **refused**: a daily bar's length depends on
   the session calendar, so deriving its close from its open would be a
   fabrication. V1 is intraday only.
5. If `bar_close_time > fired_at + clock_skew_tolerance`, the alert is rejected
   with `BAR_CLOSE_AFTER_FIRE_TIME`.

## Consequences

* Alerts configured with "Once Per Bar" instead of "Once Per Bar Close" are
  rejected rather than silently recorded as confirmed signals.
* A small clock-skew tolerance (default 10s, configurable) is needed so that
  ordinary delivery jitter is not read as pre-close firing.
* Rule 5 is a *proxy* for repaint safety, not a proof. It cannot detect an
  indicator that redraws a past arrow. That still requires the real source.
