# Engineering instructions -- NIFTY Behavioral Options Engine

## The one rule that overrides everything

**V1 never places a real order.** Do not add `place_order`, a live-order route, a
broker execution adapter, or a configuration value that could enable one.
`TRADING_MODE` is typed `Literal["PAPER"]` and `create_app` raises on anything
else; keep it that way. A contract test asserts the OpenAPI route table contains
no order or trade path.

## Strategy decisions that are final

* Half Trend Regression (HTR) on TradingView is the **only** external technical
  indicator. Python does not generate directional signals; it only ACCEPTs or
  REJECTs HTR's.
* BUY may lead to buying a CE. SELL may lead to buying a PE. Neither is a broker
  instruction.
* Do **not** add VWAP, RSI, MACD, Supertrend, moving-average crossover signals,
  previous-session directional bias, blind trend-following confirmation filters,
  unvalidated AI predictions, or stacked indicators. Historical *observations*
  are fine; yesterday-predicts-today is not.
* Rejecting every signal in a session is a valid outcome. Frequency is not a
  goal.

## Honesty requirements

* Never report a phase complete on the strength of documentation. Run it.
* Never claim an integration works when its credentials or source were
  unavailable; mark it BLOCKED or UNVERIFIED and say why.
* Never invent market data: no fabricated candles, option quotes, historical
  option prices, exchange holidays, lot sizes, expiries or strikes. If data is
  missing, fail closed and record it.
* Never present a historical win rate as a probability for the next trade, or
  simulated index-point profit as option profit.
* Every performance claim states its data period, sample size, fees and method.

## Code conventions

* Typed Python throughout; `from __future__ import annotations`.
* Network adapters stay separate from business logic; inject feeds and brokers
  so offline replay and deterministic tests are possible.
* Timestamps: store and return **UTC**; evaluate session rules in
  `Asia/Kolkata`. TradingView's `{{time}}` is a bar **open** -- derive the close
  (`app/signals/timeframes.py`), never assume it.
* Every rejection names a `ReasonCode`. Never swallow an exception silently.
* Fail closed when market data or the exchange calendar is stale, missing or
  unverified.
* Risk rules live in one place (`app/strategy/risk_manager.py` once it exists).
* Webhook handling is idempotent; the idempotency key excludes delivery
  metadata so retries collapse.
* Structured JSON logs with a `correlation_id`. Never log tokens or credentials.
* No new external service, and no broad refactor outside the current milestone,
  without a recorded decision.

## Layout

```
app/api/          FastAPI gateway + TradingView ingress
app/signals/      contracts, HTR adapter, timeframes, worker
app/market_data/  session calendar; Kite adapter (M3)
app/behavior/     behavioural features and decision engine (M4)
app/strategy/     option selector, risk manager (M5)
app/execution/    paper broker, position manager (M6)
app/research/     replay, walk-forward (M7)
app/storage/      SQLite event store
pine/             TradingView-side notes and alert templates
docs/             PRD, ARD, ADRs, TEST_PLAN, M0 audit
data/             generated; gitignored except data/reference/
tests/            unit, contract, integration, replay
```

The Node project at the repository root (`src/*.mjs`, `tests/*.test.mjs`,
`package.json`) is the earlier signal collector. It still works and is
intentionally left alone -- see `docs/ADR/0001-*`. Do not "unify" it without
authorization.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest        # 102 tests, offline
.\.venv\Scripts\python.exe -m app           # local engine on 127.0.0.1:8788
npm test                                    # pre-existing Node collector tests
```

CI must never depend on an open Indian market session. Live Kite tests are
marked `@pytest.mark.live` and skipped without explicit credentials and
authorization.

## Milestone gates

M0 audit -> M1 HTR contract -> M2 receiver -> M3 Kite data -> M4 behaviour engine
-> M5 options+risk -> M6 paper executor -> M7 forward validation. Current state
is in `docs/PRD.md`. Do not start a milestone whose predecessor's gate has not
been met, and do not move toward live broker trading.
