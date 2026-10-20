# ADR 0008 — Official sessions, Analytics Token, feed verification states, and the M4 feature engine

* Status: Accepted
* Date: 2026-10-20
* Milestones: M3 (completion), M4 (start)

## Context

Four strands, bundled because they share one discipline: evidence before
assertion, and fail-closed where evidence is missing.

## Decisions

### 1. Effective-dated official sessions (Task 1)

Holidays for both CASH and FO are now official, from NSE's own
`holiday-master` API (`type=trading`), hash-recorded in the calendar's
`sources`. The API's FO list is more current than the December circular (it adds
the 15-Jan Municipal Election), so the API is the source of record; the circular
is kept as corroboration.

F&O session timings are **effective-dated**: a window closing 15:30 up to
2026-08-02, and a second closing **15:40 from 2026-08-03**, the published NSE
extension that accompanies the Closing Auction Session. 15:35 IST is therefore
CLOSED before the effective date and OPEN on/after it, and the change is never
applied retroactively. The cash continuous session stays 15:30; CAS (15:15–15:35
for CAS-eligible stocks) is modelled as metadata, not a new continuous close.

Honesty note recorded in the file: NSE's static session-timing page did not
byte-capture (timeout), so the 15:40 window is corroborated (the page's own text
via search, plus the successfully-fetched holiday API on the same domain) rather
than captured. Muhurat 2026-11-08 keeps `UNSUPPORTED_SESSION` because NSE has
not notified its timings. Special sessions are evaluated before the weekend
rule, since Muhurat falls on a Sunday.

### 2. Analytics Token preferred (Task 2)

The free, read-only, one-year **Analytics Token** (`UPSTOX_ANALYTICS_TOKEN`) is
the preferred market-data credential: it reaches the websocket without
static-IP whitelisting and cannot place orders (GET APIs only). A normal
`UPSTOX_ACCESS_TOKEN` is the fallback. Both are registered with the log
redactor. The authorize endpoint is `/v3/feed/market-data-feed/authorize`,
returning a one-time signed websocket URL that is used immediately and never
logged. TLS verification stays on; nothing disables it, and a test asserts the
absence of `CERT_NONE` / `verify=False`.

### 3. Explicit feed-verification states (Task 3)

A connected socket is not a verified feed. `app/market_data/verification.py`
defines a lattice — `AUTHENTICATION_BLOCKED` → `AUTHENTICATION_FAILED` →
`SOCKET_CONNECTED` → `MARKET_CLOSED` → `LIVE_DATA_INVALID` →
`LIVE_STREAM_VERIFIED` → `HISTORICAL_PARITY_VERIFIED`. `LIVE_STREAM_VERIFIED`
needs validated ticks **and** a completed candle; `HISTORICAL_PARITY_VERIFIED`
needs those candles to match Upstox's own historical candles bar for bar.
`compare_candles` reports OHLC mismatches and missing/extra bars and never calls
an empty comparison parity. Tick completeness cannot be shown from a stream, so
historical candles are canonical and a missing bar is reported, never filled.

### 4. M4 behavioural feature engine (Task 6)

`app/behavior/features.py` extracts deterministic features from **completed
candles only**, with no look-ahead (the window ends at the decision bar) and no
`nan` (undefined ratios are 0.0): candle-body ratio, wick rejection, consecutive
runs, higher-high/lower-low structure, failed breakout/breakdown, momentum and
its acceleration, and volatility compression/expansion.

`app/behavior/decision_engine.py` is the ACCEPT/REJECT/ABSTAIN filter. It
generates no BUY/SELL — HTR remains the sole signal generator. Its default
decision on clean data is **REJECT with INSUFFICIENT_HISTORICAL_EVIDENCE**,
because no validated out-of-sample edge exists: `evidence_validated` defaults
False and ACCEPT is unreachable until an operator asserts it. `estimated_edge`
is always None; no win rate, probability or expectancy is manufactured. Rules
can only withhold acceptance, so adding one can only make the engine more
selective.

## Consequences

* An ordinary 2026 F&O timestamp in session hours now classifies as OPEN
  (`may_trade=True`), where before every day was `UNVERIFIED_CALENDAR`. This is
  the first time the calendar can report a tradeable session — justified by the
  official holiday data and published timings.
* The live smoke test now reports a precise `VerificationState` and only treats
  `LIVE_STREAM_VERIFIED` / `HISTORICAL_PARITY_VERIFIED` as success.
* The behavioural engine exists but trades nothing: wired into a pipeline it
  would reject every signal until evidence is validated, which is the intended
  M4-start posture.
* M1 still PARTIAL: `app/research/htr_parity.py` refuses to report parity on
  non-genuine input, and no genuine alert recording exists yet
  (`docs/M1_HTR_EVIDENCE.md`).
* Live remains BLOCKED: no credential exists and no socket has been opened.
