# Test plan and evidence

Updated 2026-10-19, end of M2.

## How to run

```powershell
# Python engine (102 tests, fully offline)
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
| Python, all | `python -m pytest` | **102 passed**, 0 failed, 1 warning |
| Python unit | `python -m pytest tests/unit` | **76 passed** |
| Python contract | `python -m pytest tests/contract` | **26 passed** |
| Node (pre-existing) | `npm test` | **6 passed**, 0 failed |

The one warning is `StarletteDeprecationWarning: Using httpx with
starlette.testclient is deprecated`, raised by FastAPI's own test client import.
It is not from this project's code.

## Coverage by area

### `tests/unit/test_timeframes.py`
TradingView interval -> canonical label for 1/5/15/60/5m/30S/D/1D/W; rejection of
empty, whitespace, `banana`, `5x`, `m5`; intraday bar durations; **refusal** to
derive a bar duration for D/W/M, because a daily bar's length depends on the
session calendar; bar-open + one bar = bar-close.

### `tests/unit/test_htr_adapter.py`
Valid BUY and SELL; legacy wordings (BULLISH/GREEN/LONG/UP, BEARISH/RED/SHORT/
DOWN); legacy field names (`direction`, `bar_time`, `index_close`); one case per
reason code -- `SCHEMA_INVALID`, `UNEXPECTED_INDICATOR`, `NOT_A_REVERSAL_EVENT`,
`UNKNOWN_SIGNAL_DIRECTION`, `UNSUPPORTED_SYMBOL`, `UNSUPPORTED_TIMEFRAME`,
`INVALID_SIGNAL_PRICE`, `MODE_NOT_PAPER`, `STALE_SIGNAL`, `FUTURE_TIMESTAMP`
(both `fired_at` and `bar_open_time`), `BAR_CLOSE_AFTER_FIRE_TIME`; the
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

## Known gaps

* **No test proves HTR does not repaint.** `BAR_CLOSE_AFTER_FIRE_TIME` rejects an
  alert that fired before its bar closed, which is a proxy, not a proof. A test
  for repainting requires the indicator's source.
* **No Kite test of any kind.** M3.
* **No behavioural, option-selection, risk, execution or replay tests.** M4-M7.
* **No test of the operator's HTTPS ingress**, including the three-second
  TradingView timeout under real network conditions.
* **No load or concurrency test** on the single-writer SQLite connection.
* The shipped exchange calendar is empty, so no test asserts correct behaviour
  on a real NSE holiday.
