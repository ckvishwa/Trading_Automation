# Architecture Reference Document

**Scope of this document:** it describes the code that exists, not the design
intent. Components marked NOT IMPLEMENTED have no code behind them. Last
updated 2026-10-19, after M1 analysis and the M3 architecture.

## Responsibility split

| Owner | Responsibility |
|---|---|
| TradingView / Pine | Signal **generation** only (Half Trend Regression reversals) |
| Python engine | Contract validation, behavioural decisions, risk, paper simulation, evaluation |
| Upstox | Live market **data** only -- adapter NOT IMPLEMENTED |
| FYERS | Historical research **data** only -- adapter NOT IMPLEMENTED |

The decision engine is deliberately separate from broker connectivity, and every
feed is reached through an interface so that offline replay and deterministic
tests are possible.

## Pipeline, with implementation status

```
TradingView (NSE:NIFTY chart, HTR indicator -- genuine Pine v5 source held
            at pine/half_trend_regression_algoalpha.pine, analysed in
            pine/HTR_ANALYSIS.md)
        |  Bullish/Bearish Trend Alert, Once Per Bar Close,
        |  JSON message authored by the operator
        v
Operator's HTTPS ingress on :443          [NOT IMPLEMENTED -- owner-provided]
        |
        v
FastAPI gateway                            [IMPLEMENTED -- M2]
  app/api/tradingview_webhook.py
  - constant-time token comparison (URL path segment, or X-Webhook-Token header)
  - body size ceiling
  - typed payload validation            app/signals/schemas.py
  - contract policy + normalization     app/signals/htr_adapter.py
  - bar-open -> bar-close derivation    app/signals/timeframes.py
  - session labelling                   app/market_data/session.py
  - durable write BEFORE acknowledgement app/storage/event_store.py
  - structured JSON logs w/ correlation id  app/logging_setup.py
        |
        v
signal_events.status = PENDING_DECISION    [IMPLEMENTED -- durable queue]
        |
        v
Signal event worker                        [OBSERVE-ONLY -- M2]
  app/signals/worker.py  logs backlog; decides nothing
        |
        v
Behavioural engine                         [NOT IMPLEMENTED -- M4]
  candle behaviour | market structure | momentum | volatility
  | pattern similarity | validated signal-outcome statistics
        ^
        |  completed candles, live index data, option quotes
Provider-neutral market-data layer         [PARTIAL -- M3]
  app/market_data/contracts.py   value types + Provenance   [IMPLEMENTED]
  app/market_data/protocols.py   provider interfaces        [IMPLEMENTED]
  app/market_data/candle_builder.py  ticks -> completed bars [IMPLEMENTED]
  app/market_data/health.py      watchdog + backoff          [IMPLEMENTED]
  app/market_data/mock_provider.py  offline fixtures         [IMPLEMENTED]
  app/market_data/providers/upstox.py   live feed       [NOT IMPLEMENTED]
  app/market_data/providers/fyers.py    research history [NOT IMPLEMENTED]
        |
        v
ACCEPT / REJECT + reason codes             [NOT IMPLEMENTED -- M4]
        v
Option selector (CE/PE, strike, expiry)    [NOT IMPLEMENTED -- M5]
        v
Risk manager                               [NOT IMPLEMENTED -- M5]
        v
Paper executor                             [NOT IMPLEMENTED -- M6]
        v
Trade journal -> performance evaluation    [NOT IMPLEMENTED -- M6/M7]
```

## Signal contracts

### Inbound (authored by the operator in TradingView's alert dialog)

`examples/htr-buy-alert.json`, `examples/htr-sell-alert.json`. Field aliases are
accepted so that alerts already configured for the earlier Node collector keep
working: `direction` -> `signal`, `bar_time`/`time` -> `bar_open_time`,
`index_close`/`close`/`price` -> `signal_price`, `timenow` -> `fired_at`, and the
wordings BULLISH/GREEN/LONG/UP -> BUY, BEARISH/RED/SHORT/DOWN -> SELL.

### Internal (`NormalizedSignalEvent`, frozen, the only thing downstream reads)

```json
{
  "schema_version": "1.0",
  "source": "tradingview",
  "indicator": "half_trend_regression",
  "indicator_version": "A9Dp4WrK",
  "symbol": "NSE:NIFTY",
  "timeframe": "5m",
  "signal": "SELL",
  "event": "REVERSAL",
  "bar_open_time": "2026-10-20T04:35:00+00:00",
  "bar_close_time": "2026-10-20T04:40:00+00:00",
  "fired_at": "2026-10-20T04:40:00+00:00",
  "received_at": "2026-10-20T04:40:30+00:00",
  "signal_price": 24000.5,
  "mode": "PAPER",
  "idempotency_key": "<sha256>"
}
```

Field values above are illustrative, not observed market prices.

**Timestamps.** Everything stored and returned is UTC. Market-session rules are
evaluated in `Asia/Kolkata`. TradingView's `{{time}}` is the triggering bar's
**open**, so `bar_close_time = bar_open_time + one bar`; this is derived in
`app/signals/timeframes.py` and tested, never assumed at a call site. Daily and
higher timeframes are refused because their bar length depends on the session
calendar.

**Idempotency key.** `sha256(indicator | symbol | timeframe | signal |
bar_close_time)`. Delivery metadata is excluded on purpose: a TradingView retry
carries a fresh `{{timenow}}` for the same bar event and must collapse onto the
same key. Primary key on `signal_events.idempotency_key` makes exactly-once
storage a database guarantee rather than an application convention.

## Storage

SQLite, WAL, `synchronous=FULL`, one write connection behind a lock.

| Table | Holds |
|---|---|
| `raw_events` | Verbatim authenticated request bodies, correlation id, content type |
| `signal_events` | Normalized events, `session_state`, `status`, full event JSON |
| `ingest_rejections` | Every ingress rejection with its reason code and detail |
| `schema_migrations` | Schema version (currently 1) |

Nothing authentication-related is written: the token travels in the URL path and
in headers, and neither is persisted or logged. `tests/contract` asserts this.

## Failure modes and the chosen behaviour

| Condition | Behaviour | HTTP |
|---|---|---|
| Wrong/absent token | Rejected, **nothing written** (an endpoint that writes on every anonymous request is a DoS surface) | 401 |
| Body over `MAX_BODY_BYTES` | Rejected before parsing | 413 |
| Unparseable JSON | Raw body stored, rejection recorded | 400 |
| Contract violation (symbol, timeframe, indicator, direction, price, mode, staleness, future timestamp, pre-close firing) | Raw body stored, rejection recorded with reason code | 200 |
| Re-delivery of a known bar event | `DUPLICATE_SIGNAL`, original `signal_id` returned | 200 |
| Bar open not on the timeframe grid measured from the IST session open | `BAR_NOT_ALIGNED_TO_TIMEFRAME` -- catches an alert created on a different timeframe | 200 |
| Accepted | Committed, then acknowledged | 200 |
| Exchange calendar not verified for the date | `session_state = UNVERIFIED_CALENDAR`; the calendar can never assert OPEN | -- |
| Process restart | Committed events survive; `PENDING_DECISION` rows are still queued; re-deliveries still duplicate | -- |

A contract violation returns 200 because TradingView treats a non-2xx as a
failed delivery, and a *correctly rejected* alert is not a delivery failure. The
reason code is in the body. A 200 means "recorded", never "will be traded".

## Market-data layer (M3, partial)

Provider-neutral by construction: the engine reads `app/market_data/contracts.py`
value types through the `app/market_data/protocols.py` interfaces, and no module
above the adapter layer names a provider. Rationale and the documented Upstox /
FYERS API facts are in `docs/ADR/0004-provider-neutral-market-data.md`.

* **Provenance on every value.** Provider, fetch time, endpoint, `is_mock`. Mock
  data cannot pass as live, and a later stage can refuse to act on mock origin.
* **Candles are built locally** from ticks, bucketed on the **IST session-open**
  grid -- the same anchor the signal contract uses, so a candle and the alert
  fired on that bar describe the same interval. A partial bar is never emitted; a
  bucket with no ticks produces no candle and is counted as skipped; a tick for
  an already-closed bucket is counted late and dropped.
* **Gaps stay gaps.** `CandleSeries` reports them and never interpolates.
* **Fail closed on feed health.** `FeedWatchdog.is_usable` is true only for an
  explicitly healthy feed; an open socket that has gone quiet reports `SILENT`.
  Reconnection uses exponential backoff with full jitter.
* **Errors, not defaults.** `DataUnavailable`, `CredentialsMissing`,
  `RateLimited`, `FeedDisconnected`. Both provider adapters currently raise
  `CredentialsMissing`.

## Research layer (M1 parity, M7 replay)

`app/research/` is **not imported by the live path**
(`docs/ADR/0005-python-htr-reference-is-research-only.md`).

* `pine_ta.py` -- `ta.linreg`, `ta.sma`, `ta.highest`, `ta.lowest`, `ta.rma`,
  `ta.atr`, plus Pine's `na` semantics: `na` propagates through
  `math.max`/`math.min`, and any comparison with `na` is false.
* `htr_reference.py` -- statement-by-statement transcription of the genuine
  source, for parity testing and historical signal reproduction. Exposes
  per-bar internal state so a parity mismatch can be diagnosed, and
  `compare_with_recorded`, which reports parity only on exact agreement.
* `replay.py` -- deterministic chronological replay of recorded alert payloads
  through the **same** adapter and store the HTTP path uses. The clock comes from
  each event, so a replay of months-old alerts is not rejected as stale, and two
  replays of one input into fresh stores produce identical summaries.

## Security posture

* Binds to 127.0.0.1. Public exposure is the operator's HTTPS ingress, on port
  443 (TradingView accepts 80/443 only), responding inside TradingView's
  three-second cancellation window. Restrict ingress to TradingView's published
  IPv4 addresses where the proxy supports it.
* Secrets come from the environment; none are in source or in git.
* `TRADING_MODE` is typed `Literal["PAPER"]`, so no configuration value can
  express a live mode, and `create_app` raises if one is injected
  programmatically. No order-placement code or route exists in the repository; a
  contract test asserts the route table.
