# Pine Script / TradingView side

The engine's only external technical indicator is **Half Trend Regression [AlgoAlpha]**
(`https://www.tradingview.com/script/A9Dp4WrK-Half-Trend-Regression-AlgoAlpha/`).

## Verification status (as of 2026-10-19)

| Item | Status | Evidence |
|---|---|---|
| Script is published open-source | VERIFIED | Script page is labelled "OPEN-SOURCE SCRIPT" |
| Pine Script version | **UNVERIFIED** | Not stated on the script page; source body is rendered client-side and was not readable from this environment |
| Exact `alertcondition()` titles / message strings | **UNVERIFIED** | Same reason |
| Inputs | PARTIAL | Page names *amplitude*, *channel deviation*, *linear regression length*; defaults not stated |
| Repaint behaviour | **UNVERIFIED** | Page says arrows are plotted on trend change; it does not state whether the change is evaluated intrabar |
| Signal confirmation timing | **MITIGATED, NOT VERIFIED** | The engine rejects any alert whose bar had not yet closed when it fired (`BAR_CLOSE_AFTER_FIRE_TIME`) |

**Nothing in this repository reimplements, infers or guesses the indicator's
calculation.** No Pine file here computes a trend. If the exact conditions
matter to a later milestone (historical signal replay in M7 / P8), the real
source is required -- see "What the owner must supply".

## How signals actually reach the engine

The alert **message body** is authored by the operator in TradingView's alert
dialog, not by Pine code. That is why the engine does not need to know the
indicator's internal variable names in order to receive its signals:

1. Open your saved **NSE:NIFTY** chart with your tuned HTR settings.
2. Create an alert whose **Condition** is the HTR indicator and whose sub-condition
   is the indicator's own **bullish trend change** alert. Do not use a generic
   price alert, and do not use a "trend is still red/green" style condition --
   a trend that is merely still red is not a new reversal.
3. Set frequency to **Once Per Bar Close**.
4. Paste the contents of `../examples/htr-buy-alert.json` as the alert message.
5. Set the webhook URL to `https://YOUR_HTTPS_HOST/v1/webhooks/tradingview/YOUR_WEBHOOK_TOKEN`.
6. Repeat for the **bearish trend change** condition with `../examples/htr-sell-alert.json`.

Notes that have bitten this flow before:

* `{{time}}` is the **opening** time of the triggering bar. The engine derives
  the bar's close time as `open + one bar` and refuses alerts that claim to
  have fired before their bar closed.
* TradingView alerts capture the indicator's inputs at creation time. If you
  change HTR's settings, **recreate both alerts**.
* TradingView accepts ports 80 and 443 only, cancels any delivery that takes
  longer than three seconds, and requires 2FA on the account for webhooks.
  Its published source addresses are 52.89.214.238, 34.212.75.30,
  54.218.53.128 and 52.32.178.7 (IPv4 only); restrict ingress to those at your
  edge where your proxy supports it.

## What the owner must supply

`htr_alert_adapter.pine` is a **template with deliberate placeholders**. It is
not runnable as shipped and must not be made runnable by guessing. To complete
it, open the indicator on TradingView, choose *Source code* / *Make a copy*, and
either:

* paste the real `alertcondition()` titles into the alert setup above (nothing
  else is then needed), or
* paste the real source into the template and connect the two genuine
  direction-change variables where the placeholders say so.

Only then can M1's repaint question be answered and historical HTR signals be
reproduced for backtesting.
