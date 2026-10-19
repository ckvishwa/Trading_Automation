# Test plan and evidence

Updated 2026-10-19, after M1 source analysis and the M3 architecture.

## How to run

```powershell
# Python engine (223 tests, fully offline)
.\.venv\Scripts\python.exe -m pytest

# Pre-existing Node signal collector (6 tests, untouched)
npm test
```

No test requires an open Indian market session, TradingView, Kite credentials,
or any network access. The clock is injected (`app.state.clock`) so freshness and
session logic are deterministic. Live Kite integration tests, when they exist,
will be marked `@pytest.mark.live` and skipped unless credentials and explicit
authorization are present.

## Results observed in this session (VERIFIED)

| Suite | Command | Result |
|---|---|---|
| Python, all | `python -m pytest` | **223 passed**, 1 skipped, 0 failed, 1 warning |
| Python unit | `python -m pytest tests/unit` | **161 passed** |
| Python contract | `python -m pytest tests/contract` | **26 passed** |
| Python replay | `python -m pytest tests/replay` | **36 passed, 1 skipped** |
| Node (pre-existing) | `npm test` | **6 passed**, 0 failed |

The one skip is deliberate and visible on purpose:
`test_parity_against_recorded_alerts_is_not_yet_possible`, marked `live`, which
skips with a message naming the recorded TradingView alerts needed to close the
M1 parity gate. It is a gap reported by the test run rather than buried here.

The one warning is `StarletteDeprecationWarning: Using httpx with
starlette.testclient is deprecated`, raised by FastAPI's own test client import.
It is not from this project's code.

## Coverage by area

### `tests/unit/test_timeframes.py`
TradingView interval -> canonical label for 1/5/15/60/5m/30S/D/1D/W; rejection of
empty, whitespace, `banana`, `5x`, `m5`; intraday bar durations; **refusal** to
derive a bar duration for D/W/M, because a daily bar's length depends on the
session calendar; bar-open + one bar = bar-close.

### `tests/unit/test_pine_source.py`
Guards on the genuine Pine file: MPL 2.0 notice and AlgoAlpha attribution
intact, `//@version=5`, both direction `alertcondition` lines byte-exact, the
Universal alert present in the original but absent from the adapter, the
reference's defaults matching the Pine inputs (2 / 2.0 / 7 / ATR 100), the two
direction gates verbatim, **`ta.atr` referenced exactly once and `channelOffset`
reaching only `upperChannel`/`lowerChannel`** (the basis of the display-only
claim), no `request.security` / `lookahead` / `barmerge`, the adapter's logic
block byte-identical to the original, and the adapter's embedded JSON messages
parsing equal to `examples/htr-{buy,sell}-alert.json`.

### `tests/unit/test_pine_ta.py`
Pine built-in semantics: `ta.linreg` returns the fitted value at the current bar
(so a straight line fits itself exactly), offsets step back along that line, a
spike is smoothed rather than tracked, `na` until the window is full; `ta.sma` /
`ta.highest` / `ta.lowest` window rules and `na` propagation; **`math.max(na, x)`
is `na`**; comparisons with `na` are false in both directions; `ta.rma` seeding;
`ta.atr` warm-up and first-bar true range.

### `tests/replay/test_htr_reference.py`
Against the genuine source: defaults match; a rise-fall-rise path yields SELL
then BUY on specific bars; signal timestamps are bar **close** times;
`trendDirection` only ever holds na/1/-1; **initialization out of `na` emits no
signal**; no signal during warm-up; `minHigh` is poisoned to `na` during warm-up;
`maxLow` is seeded from the first loaded bar's low and ratchets while the trend
is up; **Channel Deviation and ATR period cannot change a signal** while
Amplitude and Regression Length can (the control that stops the previous two
proving nothing); determinism; **no earlier signal changes when later bars are
withheld**, checked over every prefix of the series (offline repaint check);
loaded history can move the **first** flip, and signals agree once both runs have
flipped; empty input and a flat market produce nothing; the parity comparator
reports exact agreement, direction mismatches, missing and extra signals, and
**never reports parity for an empty comparison**.

### `tests/unit/test_candle_builder.py`
First bucket starts at the session open; buckets anchor on the session open, not
UTC midnight (a 10m NSE bar opens 09:15, 09:25); no candle while the bar is open;
a tick in the next bucket completes the previous bar; `close_through` emits only
once the interval has ended; **empty buckets are counted and never fabricated**;
duplicate ticks counted and excluded from OHLC; same timestamp with a different
price is not a duplicate; a tick for a closed bar is dropped as late and cannot
reach the next bar either; out-of-order ticks inside the open bar are accepted;
pre-session ticks refused; volume from the cumulative delta, from summed
quantities, or `None` when the feed publishes neither; mock provenance carried
onto the candle; a foreign symbol is a programming error; naive timestamps
refused; **the builder's close time equals the webhook's derived
`bar_close_time` for the same bar**.

### `tests/unit/test_market_data_contracts.py`
`Provenance` requires an aware timestamp and flags mock data; `Tick` rejects
naive times and non-positive prices; `Candle` rejects inverted intervals and
inconsistent OHLC; quote spread/mid only for genuinely two-sided quotes (missing
side, crossed, zero bid all excluded); **staleness is measured in both
directions**, so a future-dated quote is stale too; `CandleSeries` must be
strictly ascending and single-symbol, and reports gaps without filling them; the
mock provider satisfies the three protocols, **raises rather than inventing
data**, serves only the requested range, lists real contracts only, and refuses
to return an option from an index lookup; feed watchdog states including the
dangerous **open-but-silent** case, out-of-order messages not refreshing a
stalling feed, and jittered capped backoff.

### `tests/replay/test_signal_replay.py`
A clean run records every event once; duplicates collapse exactly as over HTTP;
events replay in `fired_at` order without hiding that the file was unordered;
**the clock comes from the event, not wall time**, so months-old alerts replay
instead of being rejected as stale; a delivery delay beyond the freshness budget
is rejected; contract violations tallied by reason code; payloads with no
timestamp replay last and are rejected; **replaying one input twice into fresh
stores is byte-identical**; replaying into a populated store records nothing new;
replayed events are queued for a decision like live ones; raw payloads stored
with a `replay` source; JSONL round trip; a malformed JSONL line raises instead
of being skipped; blank lines ignored.

### `tests/unit/test_htr_adapter.py`
Valid BUY and SELL; legacy wordings (BULLISH/GREEN/LONG/UP, BEARISH/RED/SHORT/
DOWN); legacy field names (`direction`, `bar_time`, `index_close`); one case per
reason code -- `SCHEMA_INVALID`, `UNEXPECTED_INDICATOR`, `NOT_A_REVERSAL_EVENT`,
`UNKNOWN_SIGNAL_DIRECTION`, `UNSUPPORTED_SYMBOL`, `UNSUPPORTED_TIMEFRAME`,
`INVALID_SIGNAL_PRICE`, `MODE_NOT_PAPER`, `STALE_SIGNAL`, `FUTURE_TIMESTAMP`
(both `fired_at` and `bar_open_time`), `BAR_CLOSE_AFTER_FIRE_TIME`,
`BAR_NOT_ALIGNED_TO_TIMEFRAME` (with the session-open anchor, the first bar of
the session, an off-grid bar, a pre-open bar, the 10-minute case that proves the
anchor is not UTC midnight, and the check being skipped rather than guessed when
no anchor is configured); the
delivery-age boundary from both sides; clock-skew tolerance actually applied;
non-object bodies (`None`, `[]`, string, int); idempotency key stable across
re-deliveries and distinct across direction and bar.

### `tests/unit/test_session.py`
OPEN mid-session; CLOSED before and after hours; close time exclusive; cash
pre-open distinct from regular session; weekend reported even without a verified
calendar; **CASH and FO holiday lists not shared**; an unverified calendar never
reports OPEN; dates beyond the verified horizon are unverified; naive datetimes
refused; and the calendar actually shipped in `data/reference/` is asserted to be
unverified, so invented NSE holiday data cannot slip in.

### `tests/unit/test_event_store.py`
Stored once and queued; re-delivery is a duplicate resolving to the original
`signal_id`; duplicates still detected after reopening the database; pending work
survives a restart; opposite directions on one bar are separate events;
`mark_decided` clears the queue; rejections recorded with reasons; raw bodies
retained verbatim; the worker reports each signal once; **the worker decides
nothing** (status stays `PENDING_DECISION`); stored timestamps are UTC.

### `tests/contract/test_tradingview_webhook.py`
Against the real app and a real SQLite file: valid SELL, valid BUY, both stored
exactly once; missing `signal`; unsupported symbol; unsupported timeframe;
unexpected indicator; non-reversal event; duplicate delivery stores nothing new;
stale event; future timestamp; malformed JSON (400); unauthorized (401);
unauthorized writes **nothing at all**; header-token path; missing header token;
oversized body (413); every rejection durably recorded with its reason; raw
payload recorded; **no webhook token ever persisted**; accepted signal queued
rather than traded; health reports PAPER and `live_orders: false`; the OpenAPI
route table contains **no** order or trade path; restart replays pending work and
still rejects duplicates; the app refuses to start without a token; the app
refuses any mode other than PAPER.

## Live endpoint evidence (VERIFIED, synthetic payload)

`python -m app` was started locally and exercised with curl on 2026-10-19
(outside market hours; the payload was synthetic and is labelled as such):

```
POST .../v1/webhooks/tradingview/<token>  -> 200 {"status":"SIGNAL_RECORDED","signal":"SELL",
   "timeframe":"5m","bar_close_time":"2026-10-19T18:59:00+00:00",
   "session_state":"UNVERIFIED_CALENDAR","mode":"PAPER"}
same body again                           -> 200 {"reason":"DUPLICATE_SIGNAL", same signal_id}
wrong token                               -> 401 {"reason":"UNAUTHORIZED"}
'{nope'                                   -> 400 {"reason":"MALFORMED_JSON"}
ticker BANKNIFTY                          -> 200 {"reason":"UNSUPPORTED_SYMBOL"}
GET /health -> signals_recorded: 1, pending_decisions: 1, decision_engine: NOT_IMPLEMENTED
```

Structured JSON logs carried a `correlation_id` on every line, and the worker
logged `signal awaiting behavioral decision ... decision_engine: NOT_IMPLEMENTED`.
The smoke-test database was deleted afterwards so the journal starts empty.

### Second run, after the bar-grid alignment check was added

```
bar_open 2026-10-19T19:25:00+00:00 (00:55 IST)  -> 200 BAR_NOT_ALIGNED_TO_TIMEFRAME
   detail: "-500 min from that day's 09:15 IST session open, not a whole number of 5m bars"
wrong token                                     -> 401 UNAUTHORIZED
'{nope'                                         -> 400 MALFORMED_JSON
payload with no "signal" field                  -> 200 SCHEMA_INVALID ("signal: Field required")
```

The first line is the check working, not failing: 00:55 IST is not an NSE bar.
A live POST of a genuine in-session bar is impossible outside market hours,
because the freshness budget and the session grid cannot both be satisfied, so
in-session behaviour was exercised through the replay CLI instead:

```
python -m app.research replay --jsonl examples/replay/htr-session-sample.jsonl
  -> total 4, recorded 3, duplicates 1, rejected 0, reasons {DUPLICATE_SIGNAL: 1}
GET /health  -> signals_recorded: 3, pending_decisions: 3
worker log   -> 3x "signal awaiting behavioral decision", one per recorded signal
```

Four synthetic payloads on genuine 09:15-anchored 5-minute bars (09:50, 10:50,
12:05 IST, plus a re-delivery of the first). Three recorded, the re-delivery
collapsed as a duplicate, none rejected -- so the alignment check accepts real
NSE bar timestamps, and the running engine's worker picked all three up from the
durable queue. The journal was cleared again afterwards.

## Known gaps

* **No parity against genuine TradingView alerts.** This is the substantive M1
  gap. The reference agrees with the *source as read*; nothing yet shows it
  agrees with what TradingView actually fired. Needs recorded alerts: bar close
  time and direction per event, with the chart's symbol, timeframe, indicator
  inputs and history start.
* **No live alert has ever been received.** Every webhook result recorded so far
  came from a synthetic payload.
* **Historical repaint is ruled out offline; intrabar flicker is not measured.**
  The prefix test shows a closed bar's signal never changes. The forming bar can
  still flip and unflip, and its rate on live NIFTY data is unknown.
* **The adapter Pine copy has not been compiled.** Its logic block is asserted
  byte-identical to the original, but no TradingView compile has been run.
* **No provider adapter exists.** Upstox and FYERS both raise
  `CredentialsMissing`; their documented facts are recorded but unconfirmed
  against a live account, and neither rate-limit page has been read.
* **No behavioural, option-selection, risk or execution tests.** M4-M6. Replay
  covers ingestion only: no outcomes, no P&L.
* **No test of the operator's HTTPS ingress**, including the three-second
  TradingView timeout under real network conditions.
* **No load or concurrency test** on the single-writer SQLite connection.
* The shipped exchange calendar is empty, so no test asserts correct behaviour
  on a real NSE holiday.
