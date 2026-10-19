# Pine Script / TradingView side

The engine's only external technical indicator is **Half Trend Regression
[AlgoAlpha]** (`https://www.tradingview.com/script/A9Dp4WrK-Half-Trend-Regression-AlgoAlpha/`).

| File | What it is |
|---|---|
| `half_trend_regression_algoalpha.pine` | The genuine source, saved verbatim as supplied by the owner. © AlgoAlpha, MPL 2.0. **Do not edit.** |
| `HTR_ANALYSIS.md` | Line-by-line analysis: conditions, defaults, state machine, repaint assessment, alert setup |
| `htr_alert_adapter.pine` | Optional modified copy under MPL 2.0 that embeds the webhook JSON in `message=`. Logic byte-identical to the original |

## Verification status (2026-10-19)

| Item | Status | Evidence |
|---|---|---|
| Source obtained | **VERIFIED** | `half_trend_regression_algoalpha.pine`, supplied by the owner |
| Pine version | **VERIFIED** | `//@version=5` |
| BUY / SELL conditions | **VERIFIED** | `ta.crossover(trendDirection, 0)` → BUY; `ta.crossunder(trendDirection, 0)` → SELL |
| Alert condition names | **VERIFIED** | "Bullish Trend Alert", "Bearish Trend Alert", "Universal Trend Alert" (the last is unusable — fires both ways) |
| Default inputs | **VERIFIED** | amplitude 2, channel deviation 2.0, regression length 7, ATR period hardcoded 100 |
| Channel Deviation / ATR affect display only | **VERIFIED** | `channelOffset` reaches only `upperChannel`/`lowerChannel`, used only in plots, fills and label placement. Pinned by `test_atr_is_referenced_exactly_once_and_only_for_the_channel` and `test_channel_deviation_cannot_change_a_signal` |
| Historical bars do not repaint | **VERIFIED (offline)** | No `request.security`, no forward offsets; every prefix of a series reproduces the same signals (`test_withholding_later_bars_never_changes_an_earlier_signal`) |
| Forming bar can flicker | **VERIFIED (by analysis)** | `ta.linreg(..., 0)` recomputes on the open bar, so `trendDirection` can flip and unflip intrabar. Mitigated by requiring `Once Per Bar Close` and by rejecting pre-close firing |
| Persistent-state initialization | **VERIFIED** | `trendDirection` na → 1 at bar index `linreglen + 1`, with **no** alert (`ta.crossover` against na is false) |
| Warm-up / history-length sensitivity | **VERIFIED, and bounded** | `maxLow` is seeded from the first loaded bar's low, so the **first** flip can move with loaded history; signals after both runs have flipped agree exactly |
| Python parity vs genuine TradingView alerts | **NOT DONE** | Needs recorded real alerts. `test_parity_against_recorded_alerts_is_not_yet_possible` is marked `live` and skips |
| Live alert observed end to end | **NOT DONE** | No alert has yet been received from TradingView; all webhook evidence so far is synthetic |
| Adapter copy compiles on TradingView | **NOT VERIFIED** | Cannot be compiled from here. Paste it into the Pine editor once if you intend to use it |
| Intrabar flicker rate on live NIFTY | **NOT MEASURED** | Needs live observation |

## Alert setup

Two alerts per chart. **The original indicator needs no edit** — the webhook JSON
is typed into TradingView's alert dialog, which is why the engine's contract does
not depend on the indicator's internal variable names.

1. Open your saved **NSE:NIFTY** chart with your own HTR settings.
2. Create alert → Condition **AlgoAlpha - Half Trend** → **Bullish Trend Alert**.
3. Frequency **Once Per Bar Close**. This is not optional: see the flicker row above.
4. Message: paste `../examples/htr-buy-alert.json`.
5. Webhook URL: `https://YOUR_HTTPS_HOST/v1/webhooks/tradingview/YOUR_WEBHOOK_TOKEN`.
6. Repeat with **Bearish Trend Alert** and `../examples/htr-sell-alert.json`.

Do not select *Universal Trend Alert*: it fires on both directions and the
payload could not say which happened.

Gotchas that are enforced in code, not left to memory:

* `{{time}}` is the triggering bar's **opening** time. The engine derives the
  close as `open + one bar` and rejects an alert that fired before its bar closed.
* A bar open that is not a whole number of bars after the 09:15 IST session open
  is rejected (`BAR_NOT_ALIGNED_TO_TIMEFRAME`) — this catches an alert created on
  a different timeframe from the one configured.
* TradingView captures the indicator's inputs when the alert is created.
  **Changing Amplitude or Linear Regression Length means recreating both alerts.**
  Changing Channel Deviation does not: it cannot move a signal.

Platform constraints: ports 80 and 443 only, three-second response timeout,
account 2FA required for webhooks, IPv4 only. Published source addresses are
52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7 — restrict ingress to
those at your edge where your proxy supports it.

## What is still needed from the owner

1. **A recording of genuine alerts** — bar close time and direction per event,
   with the chart's symbol, timeframe, indicator inputs and history start. That
   closes the parity gate and is the last substantive M1 item.
2. **One live alert delivered end to end** through your HTTPS ingress, to confirm
   the timestamp and direction mapping against something other than a synthetic
   payload.
