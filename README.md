# NIFTY 50 Behavioral Options Engine

This repository now holds **two** things. Read this first so you run the right one.

| | Python engine (`app/`) | Node collector (`src/`) |
|---|---|---|
| Role | The V1 behavioral options engine under active development | The earlier signal-only observer, still working, left untouched |
| Run | `.\.venv\Scripts\python.exe -m app` | `npm run listen` |
| Port | 8788 | 8787 |
| Webhook path | `/v1/webhooks/tradingview/<WEBHOOK_TOKEN>` | `/alert/<ALERT_KEY>` |
| Store | `data/engine.db` (SQLite WAL) | `data/accepted-signals.jsonl` |
| Tests | `.\.venv\Scripts\python.exe -m pytest` (102) | `npm test` (6) |

Point your TradingView alert at **one** of them. Running both against the same
alert double-counts the signal in two separate journals.

**Neither places a real order.** The Python engine has no order-placement code,
no live-order route, and no configuration value that could enable one.

## Python engine quick start

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env      # if you do not already have a .env
# set WEBHOOK_TOKEN to 32+ random characters:
#   $b=New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Fill($b); [Convert]::ToHexString($b)
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m app
Invoke-RestMethod http://127.0.0.1:8788/health
```

It binds to loopback. TradingView cannot reach it without your own
authenticated HTTPS ingress on port 443 (TradingView accepts ports 80/443 only
and cancels deliveries slower than three seconds).

Alert setup, including the exact alert-message JSON and the verification status
of the Half Trend Regression indicator, is in [`pine/README.md`](pine/README.md).

### Current state

| Milestone | Status |
|---|---|
| M0 repository audit | PASS -- [`docs/AUDIT_M0.md`](docs/AUDIT_M0.md) |
| M1 HTR signal contract | PARTIAL -- transport contract tested; indicator source, Pine version, alert-condition names and repaint behaviour **unverified** |
| M2 FastAPI receiver | PASS -- 102 tests, live endpoint exercised |
| M3 Kite market data onward | NOT STARTED |

The exchange calendar in `data/reference/nse_calendar.json` ships **empty on
purpose**: no NSE holiday data was invented. Until you fill it in from official
NSE circulars and set `verified_through`, the session module reports
`UNVERIFIED_CALENDAR` and downstream gates fail closed.

Docs: [PRD](docs/PRD.md) | [ARD](docs/ARD.md) | [Test plan](docs/TEST_PLAN.md) |
[ADRs](docs/ADR/) | [Engineering instructions](CLAUDE.md)

---

# NIFTY 50 AlgoAlpha Signal Observer (Phase 1)

For a combined NIFTY/SENSEX collector, set `EXPECTED_SYMBOLS=NSE:NIFTY,BSE:SENSEX` in `.env` and create separate TradingView reversal alerts on each index chart. The default remains NSE:NIFTY. This only expands the signal allowlist; it does not enable trades.

Phase 2 offline option replay is delivered separately in the Codex task's `outputs/paper_options_simulator` folder. Its `--journal` option reads this project's `data/accepted-signals.jsonl` and requires independent historical option quote snapshots. No real option fills or backtest claims are present in this Phase 1 project.

**Safety:** This is a **signal-only** prototype; it never submits Kite orders, clicks Buy/Sell, simulates fills, or uses financial account credentials in code. It does not claim the indicator is profitable. Playwright displays your sites and captures local screenshots. TradingView **indicator alerts** provide authoritative reversal events.

## Prerequisites
- Windows PowerShell, Node.js 20+, npm
- TradingView account with *Half Trend Regression [AlgoAlpha]* added to your saved **NSE:NIFTY** chart
- Set your existing chart interval and indicator input values yourself; the script does not modify them
- TradingView webhook access and 2FA if you want real webhooks (check your plan)
- For future live execution (NOT implemented here): a compliant Kite Connect setup, Zerodha-registered static IP, risk framework, and exchange/regulatory requirements

## 1. Install (PowerShell)

```powershell
cd .\nifty-scalper-mvp
npm install
npx playwright install chromium
Copy-Item .env.example .env
```

Edit `.env`, set **ALERT_KEY** to a new 32+ character random value and `EXPECTED_INTERVAL` to your chart timeframe, e.g. `1` or `3`. Example PowerShell generator:

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
[Convert]::ToHexString($bytes)
```

Keep `.env`, `browser_profiles`, `screenshots`, and `data` private and out of git. If the output folder is inside a git project, `.gitignore` already excludes them.

## 2. Open read-only browser sessions

```powershell
npm run watch
```

Two headed Chromium windows open TradingView and Kite. Log into each manually; 2FA is never bypassed. On TradingView select **your existing saved chart layout** including your tuned AlgoAlpha settings. Browser windows are **not** a buy/sell execution path. Every 60 seconds, latest local screenshots are overwritten in `screenshots/`.

## 3. Start local webhook listener (second PowerShell window)

```powershell
npm run listen
# Verify in a third terminal:
Invoke-RestMethod http://127.0.0.1:8787/health
```

Webhook is bound to loopback only, so TradingView cannot directly reach it. Publish it using **your own authenticated HTTPS reverse proxy or controlled tunnel** on port 443. Do not expose localhost blindly or enable unauthenticated public access. A webhook path `https://YOUR_HTTPS_HOST/alert/YOUR_ALERT_KEY` forwards to the local listener; use access controls and restrict ingress to the [TradingView published webhook IPs](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/) at your edge where possible.

### Alert configuration in TradingView

1. On the **NSE:NIFTY** chart, confirm the exact indicator is `Half Trend Regression [AlgoAlpha]` and retain your custom settings.
2. Click Create alert. Select the indicator's **bearish reversal** condition (do **not** select a generic price alert or "trend is red"). Frequency: **Once Per Bar Close** if the selected indicator condition allows it.
3. In the alert message paste the full JSON from `examples/tradingview-bearish-alert.json` (with `BEARISH` constant). Enable webhook URL pointing at the secured URL above.
4. Create a **separate bullish reversal** alert with `examples/tradingview-bullish-alert.json`.
5. If you change the indicator inputs, **recreate both alerts**: TradingView alerts otherwise retain the old settings.

TradingView substitutes `{{exchange}}`, `{{ticker}}`, `{{interval}}`, `{{close}}`, `{{time}}`, and `{{timenow}}` at delivery. Configure TradingView 2FA for webhooks. Your public listener must respond within 3 seconds. Review the alert log for delivery failures.

## 4. Validate

```powershell
npm test
```

Accepted fresh, authorized alerts are appended to `data/accepted-signals.jsonl`; duplicate, stale, wrong-symbol/timeframe, weekend, and out-of-session alerts are rejected. **There are no paper fills or orders**; signal acceptance is not equivalent to a profitable strategy.

Optional local-only artificial test during off-hours: temporarily set `ALLOW_AFTERHOURS_PAPER=true`, restart listener, submit a synthetic alert with current `fired_at` and `bar_time` timestamps to `http://127.0.0.1:8787/alert/YOUR_ALERT_KEY`, then restore false. Never forward actual broker orders for this test. Always use accurate event timestamps.

## Scope boundaries

- The NIFTY 50 **index cannot itself be bought/sold**. Decide later whether the tradable instrument is a NIFTY future, a specific option contract, or something else, with explicit risk limits and liquidity checks.
- A red trend **is not** a new bearish reversal. Only actual indicator reversal alerts count.
- This prototype does not reconcile Kite orderbook/positions, calculate actual net P&L, use intraday broker feed, check special sessions or exchange holidays, or calculate slippage. Those are necessary for a production executor.
- The journal's `MAX_SIGNALS_PER_DAY` is a signal cap only; it is **not** a real trading loss limit.
- TradingView webhook can fail; use retries/reconciliation and alert logs in any future production system.
- Do not use Playwright pixel matching, account password scraping, direct browser Buy/Sell clicks, or broker's undocumented endpoints for live scalping.

## Phase 2 (not built yet)

Decide *instrument*, *timeframe*, *signal-to-position mapping*, entry rules, stop, target, max loss, maximum trades, latency constraints, compliance, and data subscription. Then add replayed candle-data backtesting, cost/slippage estimates, paper broker simulation with market data, and Kite Connect broker integration behind a manual armed switch and static IP compliance. Do not promote to live until the results are verified and the required controls exist.

### Primary documentation
- Indicator: https://www.tradingview.com/script/A9Dp4WrK-Half-Trend-Regression-AlgoAlpha/
- Webhooks: https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/
- TradingView placeholder variables: https://www.tradingview.com/support/solutions/43000531021-how-to-use-a-variable-value-in-alert/
- Kite Connect: https://kite.trade/docs/connect/v3/
- Zerodha static IP: https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/static-ip
