# ADR 0006 — Calendar source trust, and splitting the freshness budgets

* Status: Accepted
* Date: 2026-10-19
* Milestones: M1 (transport telemetry), M3/M4 (session gating)

## Context

Two separate problems, both caused by one value standing in for several facts.

**The calendar.** `nse_calendar.json` previously had a single
`verified_through` field and a flat holiday list. NSE's own site does not
respond to automated retrieval (two 60-second timeouts on 2026-10-19), so the
only data obtainable here came from third-party broker holiday pages. Those
pages agree that Tuesday 2026-10-20 is Dussehra and closed, and disagree about
the Muhurat trading date. Writing that into the file as though it had been read
from a circular would be a fabricated verification; leaving it out would mean
the engine does not know about a closure it has good evidence for.

**The tolerances.** One `clock_skew_tolerance_seconds` of 10s was being used
both for "this timestamp is in the future" and for "this alert fired before its
bar closed", and one `max_signal_age_seconds` decided both "too late to trade"
and "too late to keep". Those are four different questions. In particular,
rejecting a late-but-genuine signal outright destroys the evidence needed to
measure how often the transport is slow — and biases the measurement towards
the deliveries that happened to be fast.

## Decision

### Calendar: unofficial data may close the market, never open it

Calendar schema version 2. Each session window and each holiday or special
session carries a `source` of `official`, `unofficial` or `disputed`, and the
file carries `official_verified_through`.

* `OPEN` and `PRE_OPEN` require **both** a date inside
  `official_verified_through` **and** a session window marked `official`.
* `HOLIDAY` and `WEEKEND` are reported whatever the source. An unofficial
  holiday still closes the day.
* A special session that is not official, or whose timings are null, yields
  `UNSUPPORTED_SESSION`. Regular hours are never assumed in its place.
* Session timings are **effective-dated**: each segment holds a list of windows
  with an `effective_from`, so a timing change does not rewrite history. A date
  before every window yields `UNSUPPORTED_SESSION`, and asking for the bar-grid
  anchor raises.
* Cash and derivatives stay separate segments.

The asymmetry is deliberate: being wrongly closed costs a missed trade, being
wrongly open costs a trade placed into a market that was not there.

The repository therefore ships the third-party 2026 closures it has evidence
for, each marked `unofficial` with its evidence noted, with
`official_verified_through: null` and `holiday_coverage: "partial"`. The engine
consequently reports no session as open from the shipped file, which is correct
until the owner reads the circulars.

### Transport: four budgets, and three freshness states

| Setting | Question it answers | Default |
|---|---|---|
| `MAX_TRADABLE_AGE_SECONDS` | How late may a delivery be and still open a paper position? | 120 |
| `MAX_RECORDABLE_AGE_SECONDS` | How late may a delivery be and still be worth storing? | 900 |
| `CLOCK_SKEW_TOLERANCE_SECONDS` | How far into the future may a timestamp sit before it is a clock fault? | 10 |
| `BAR_CLOSE_TOLERANCE_SECONDS` | How far before its own bar close may an alert claim to have fired? | 5 |

A delivery is then `FRESH` (tradable), `DELAYED` (recorded, `tradable=False`)
or `EXPIRED` (rejected, `STALE_SIGNAL`). Config refuses a recordable budget
smaller than the tradable one.

Candle alignment is enforced separately and for its own reasons:
`BAR_CLOSE_AFTER_FIRE_TIME` when an alert fired before its bar closed, and
`BAR_NOT_ALIGNED_TO_TIMEFRAME` when the bar open is not a whole number of bars
after the IST session open.

### Telemetry

Every recorded signal stores `bar_open_time`, `bar_close_time`, `fired_at`,
`received_at`, `processed_at`, `delivery_delay_ms`, `bar_close_lag_ms`,
`processing_ms`, `freshness`, `tradable` and `session_state`, and the same
fields appear in one structured log line per signal. Processing time is measured
with a monotonic clock so a wall-clock adjustment mid-request cannot produce a
negative duration. `GET /v1/telemetry/signals` reads them back.

Storing them on the event rather than relying on logs is the point: every later
question about latency is answerable from the journal, after the logs have
rotated away.

## Consequences

* The shipped calendar reports `UNVERIFIED_CALENDAR` for every ordinary trading
  day, and `HOLIDAY` for 2026-10-20. Live trading decisions need the owner to
  promote the file to `official`.
* Event-store schema moves to version 2, applied as additive `ALTER TABLE`
  statements so an existing journal is upgraded in place.
* A late signal now reaches the journal where it previously did not. Downstream
  stages must check `tradable`, not merely that a signal exists — the paper
  executor must refuse to open a position on a `DELAYED` event.
* Four settings instead of two is more configuration surface, accepted because
  one tolerance silently doing two jobs is how a repainting bar gets treated as
  confirmed.
