# M0 -- Repository audit

Audited: 2026-10-19. Working directory `E:\Trading_Automation`.
Everything below marked VERIFIED was observed by running the command or reading
the file in this session. Assumptions are labelled as such.

## Baseline that existed before this session (VERIFIED)

A Node.js project, `nifty-scalper-mvp` v0.1.0, ESM, Node >= 20:

```
.env                .env.example         .gitignore         package.json
package-lock.json   README.md (6.9 KB)
src/browser.mjs         Playwright viewer: opens TradingView + Kite tabs, 60s screenshots
src/signal-engine.mjs   checkSignal() contract validation + SignalJournal (JSONL)
src/webhook-server.mjs  node:http listener on 127.0.0.1, token-in-path auth
tests/signal-engine.test.mjs   6 node:test cases
examples/tradingview-{bullish,bearish}-alert.json
browser_profiles/{tradingview,kite}/   persistent Chromium profiles (gitignored)
screenshots/kite-latest.png, tradingview-latest.png
```

* `npm test` -> **6 passed, 0 failed** (VERIFIED, run in this session).
* No git repository existed (`fatal: not a git repository`).
* No `data/` directory existed, so no signals had ever been journaled.
* No Pine Script file anywhere; `find . -iname '*pine*'` returned nothing.
* No Kite reference anywhere; `grep -ril "kiteconnect\|kite_connect"` returned
  nothing outside `node_modules`. Kite exists only as a browser tab in
  `browser.mjs` and as prose in the README.
* Node v24.13.1, Python 3.12.10, git 2.53.0 all present.

The existing Node code is honest about its scope: the README states it is
"signal-only", never submits orders, and that `MAX_SIGNALS_PER_DAY` is a signal
cap and not a loss limit. Its validation rules (symbol allowlist, timeframe,
delivery-age, restart-safe dedup, IST session window) were read and their intent
carried forward.

## Component status

| Component | Existing | Working | Missing | Action taken |
|---|---|---|---|---|
| Pine Script integration | No | -- | Source, exact alert conditions, repaint status | `pine/` added: verification table + alert-message templates + a deliberately non-runnable adapter template. Indicator logic **not** reimplemented. |
| Webhook receiver | Yes (Node, loopback) | Yes (6 tests) | Typed contract, durable store, bar-close normalization, correlation ids | New FastAPI receiver at `POST /v1/webhooks/tradingview[/{token}]`, SQLite WAL store, 26 contract tests |
| Kite market data | No | -- | Everything (auth, instruments, ticks, candles, option quotes) | **Not started.** M3. |
| Historical storage | Partial (JSONL append) | Yes | Queryable store, raw+normalized separation, replay | SQLite WAL with `raw_events`, `signal_events`, `ingest_rejections`, schema version |
| Behavior engine | No | -- | Everything | **Not started.** M4. Reason codes defined; worker is observe-only. |
| Risk manager | No | -- | Everything | **Not started.** M5. |
| Paper executor | No | -- | Everything | **Not started.** M6. No order code exists anywhere. |
| Tests | Yes (6, Node) | Yes | Python unit/contract/integration/replay | 102 Python tests added; Node tests left untouched and still passing |

## Material finding: two stacks

The agreed V1 stack is Python 3.12 + FastAPI + NumPy/pandas + SQLite/Parquet +
pytest. The existing project is Node.js. The behavioral engine, walk-forward
research and replay harness all live in the Python ecosystem, so the Python
stack was built out and the Node project was left **in place and untouched**.
See `docs/ADR/0001-python-stack-alongside-existing-node-collector.md`.

Nothing was deleted, moved or rewritten. One additive change was made to a
user-owned file: a `WEBHOOK_TOKEN` (plus `TRADING_MODE=PAPER`, `API_PORT=8788`)
was appended to `.env`, which previously had no key the Python app could start
with. The pre-existing `ALERT_KEY` and all other values are unchanged.

## Risks recorded at M0

1. **HTR repaint behaviour is unverified.** Mitigated, not solved: the engine
   rejects alerts that fired before their bar closed. It cannot detect an
   indicator that redraws a past arrow. Resolving this needs the real source.
2. **No verified exchange calendar.** `data/reference/nse_calendar.json` ships
   with empty holiday lists and `verified_through: null`, and the session module
   therefore reports `UNVERIFIED_CALENDAR` instead of `OPEN`. No NSE holiday
   data was invented.
3. **No Kite credentials seen**, so no live market data claim is made.
4. **Two parallel codebases** is a maintenance cost until the Node collector is
   retired by owner decision.
5. **Public ingress is the owner's responsibility.** The app binds to
   127.0.0.1; TradingView cannot reach it without an HTTPS proxy on 443.
