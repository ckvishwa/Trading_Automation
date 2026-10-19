# Half Trend Regression [AlgoAlpha] — source analysis

Source of truth: `half_trend_regression_algoalpha.pine` in this directory, saved
verbatim as supplied by the owner. © AlgoAlpha, Mozilla Public License 2.0
(https://mozilla.org/MPL/2.0/). Pine Script **v5** (`//@version=5`).

Everything below was read out of that file. Nothing is inferred from the script's
description page.

## 1. Inputs and their published defaults

| Input | Pine name | Default | Feeds the signal? |
|---|---|---|---|
| Amplitude | `amplitude` | `2` | **Yes** — `ta.sma`, `ta.highest`, `ta.lowest` length |
| Channel Deviation | `channelDeviation` | `2.0` (min `0.1`) | **No** — channel width only |
| Linear Regression Length | `linreglen` | `7` | **Yes** — `ta.linreg` length, and the warm-up gate |
| Bullish / Bearish Color | `green` / `red` | `#00ffbb` / `#ff1100` | No |
| ATR period | hardcoded `atrPeriod = 100` | `100` | **No** — channel width only |

## 2. The genuine signal conditions

```pine
alertcondition(ta.crossunder(trendDirection, 0), title="Bearish Trend Alert")
alertcondition(ta.crossover(trendDirection, 0),  title="Bullish Trend Alert")
alertcondition(ta.cross(trendDirection, 0),      title="Universal Trend Alert")
```

| Alert condition to select in TradingView | Engine signal | Transition |
|---|---|---|
| **Bullish Trend Alert** | `BUY` | `trendDirection` −1 → 1 |
| **Bearish Trend Alert** | `SELL` | `trendDirection` 1 → −1 |
| Universal Trend Alert | **do not use** | either direction; the payload could not state which |

`trendDirection` only ever holds `na`, `1` or `-1`, so a sign change across zero
is exactly a trend flip. The two `plotshape` calls titled "Bearish Trend" and
"Bullish Trend" draw the ▼ / ▲ labels from the same conditions; they are not
alert conditions.

None of the three `alertcondition()` calls passes a `message=` argument, so the
webhook payload is typed into the alert dialog's *Message* box. That is why the
engine's contract does not depend on any Pine edit — see §7.

## 3. State machine

Persistent (`var`) state: `trendLine`, `trendDirection`, `maxLow`, `minHigh`.
Seeds: `trendDirection = na`, `maxLow = low` and `minHigh = high` **of the first
loaded bar**, `trendLine = na`.

Per-bar inputs:

```pine
highs = ta.linreg(high,  linreglen, 0)      // offset 0 = current bar
lows  = ta.linreg(low,   linreglen, 0)
closes= ta.linreg(close, linreglen, 0)
highMA = ta.sma(highs, amplitude)       lowMA  = ta.sma(lows, amplitude)
highestHigh = ta.highest(highs, amplitude)  lowestLow = ta.lowest(lows, amplitude)
```

Transitions, in the order the source evaluates them:

1. **Initialization.** `na(trendDirection) and bar_index > linreglen` →
   `trendDirection := 1`. With `linreglen = 7` this is bar index 8.
   Produces **no alert**: `ta.crossover` against a `na` previous value is false.
2. **Up → down (SELL).** While `trendDirection == 1`:
   `maxLow := math.max(maxLow, lowestLow)`, then flip if
   `highMA < maxLow and closes < lows[1]`. On flip: `trendDirection := -1`,
   `trendLine := highestHigh`, `minHigh := highs`.
3. **Down → up (BUY).** Otherwise (`trendDirection` is `-1` **or still `na`** —
   `na == 1` is false in Pine, so warm-up bars run this branch):
   `minHigh := math.min(minHigh, highestHigh)`, then flip if
   `lowMA > minHigh and closes > highs[1]`. On flip: `trendDirection := 1`,
   `trendLine := lowestLow`, `maxLow := lows`.
4. **Clamp (display only).** `trendLine` is then clamped to `lowestLow` /
   `highestHigh` depending on direction. It feeds plots and fills, never a gate.

Two consequences worth stating plainly:

* **Warm-up is silent.** `ta.highest(highs, 2)` is `na` while `highs` is `na`
  (the first six bars), and Pine's `math.min(minHigh, na)` is `na`, so `minHigh`
  is poisoned to `na` on bar 0 and the `lowMA > minHigh` gate cannot fire. No
  flip, and therefore no alert, is possible before initialization.
* **`maxLow` carries the first loaded bar's low** until the first flip reseeds
  it. More or less loaded chart history can therefore move the **first** flip.
  After any flip, both `maxLow` and `minHigh` are reseeded from that bar, so
  later signals do not depend on how far back the chart goes. Pinned by
  `tests/replay/test_htr_reference.py::test_loaded_history_can_move_the_first_flip`
  and `::test_signals_agree_once_both_runs_have_flipped_once`.

## 4. Channel Deviation and ATR do not affect direction — verified

```pine
halfATR       = ta.atr(atrPeriod) / 2
channelOffset = channelDeviation * halfATR
upperChannel  = trendLine + channelOffset
lowerChannel  = trendLine - channelOffset
```

`channelOffset` appears only in `upperChannel` / `lowerChannel`. Those two appear
only in `plot(...)`, `fill(...)` and as the **vertical placement** of the ▲ / ▼
labels (`ta.crossover(trendDirection, 0) ? lowerChannel : na`). The flip gates in
§3 read `highMA`, `maxLow`, `closes`, `lows[1]`, `lowMA`, `minHigh`, `highs[1]`
and nothing else. `ta.atr` is not referenced anywhere else in the file.

So changing Channel Deviation moves where a label is drawn, never whether one is
drawn. Regression tests:
`test_channel_deviation_cannot_change_a_signal`, `test_atr_period_cannot_change_a_signal`,
with `test_amplitude_and_regression_length_do_change_signals` as the control.

## 5. Repaint behaviour — assessed

| Question | Answer | Basis |
|---|---|---|
| Can a **closed** bar's signal change later? | **No** | `ta.linreg(..., 0)` and the window functions read the current and prior bars only; no `request.security`, no future offsets. The reference reproduces this: `test_withholding_later_bars_never_changes_an_earlier_signal` runs every prefix of a series and finds no signal moved, added or removed. |
| Can the signal flicker on the **forming** bar? | **Yes** | `offset = 0` means `highs`/`lows`/`closes` are recomputed on every tick of the unclosed bar, so `trendDirection` can flip and unflip intrabar. Pine rolls `var` state back to the last bar close on each realtime recalculation, so the flicker does not corrupt committed state — but an intrabar-firing alert can report a flip that does not survive the close. |
| Does the indicator look into the future? | **No** | Only `[1]` back-references appear. |

**Therefore: the alert must be created with frequency `Once Per Bar Close`.**
The engine does not rely on the operator getting that right: it derives the bar's
close from TradingView's `{{time}}` (a bar **open**) and rejects any alert that
fired before its bar closed (`BAR_CLOSE_AFTER_FIRE_TIME`), and separately rejects
a bar open that is not on the timeframe grid measured from the session open
(`BAR_NOT_ALIGNED_TO_TIMEFRAME`).

What is still **unverified**: the intrabar flicker rate on live NIFTY data, and
whether TradingView's delivery of an `alertcondition` alert ever lands before the
bar-close timestamp by more than the configured skew tolerance. Both need live
observation; neither can be settled from the source.

## 6. Python reference implementation

`app/research/htr_reference.py` transcribes §3 statement by statement, using
`app/research/pine_ta.py` for `ta.linreg`, `ta.sma`, `ta.highest`, `ta.lowest`
and Pine's `na` semantics (`na` propagates through `math.max`/`math.min`; any
comparison with `na` is false). It is used for offline parity testing and
historical signal reproduction. **It does not generate live signals** — the live
architecture is unchanged: TradingView fires the alert, the webhook records it.

Parity against genuine TradingView alerts is **not yet done**: it needs a
recorded list of real alert events (bar close time + direction) from the owner's
chart, with that chart's symbol, timeframe, inputs and history start.
`tests/replay/test_htr_reference.py::test_parity_against_recorded_alerts_is_not_yet_possible`
is marked `live` and skips, naming exactly what is missing.

## 7. Alert setup

Two alerts per chart. Nothing in the indicator needs editing.

1. Open your saved **NSE:NIFTY** chart with your own HTR settings.
2. Create alert → Condition: **AlgoAlpha - Half Trend** → **Bullish Trend Alert**.
3. Frequency: **Once Per Bar Close**.
4. Message: paste `../examples/htr-buy-alert.json`.
5. Webhook URL: `https://YOUR_HTTPS_HOST/v1/webhooks/tradingview/YOUR_WEBHOOK_TOKEN`.
6. Repeat with **Bearish Trend Alert** and `../examples/htr-sell-alert.json`.

Do not use *Universal Trend Alert*: it fires on both directions, and the payload
could not say which one happened.

TradingView captures the indicator's inputs when the alert is created. **If you
change Amplitude or Linear Regression Length, delete and recreate both alerts**,
or they keep firing on the old settings. Changing Channel Deviation cannot change
when they fire (§4), so it does not require recreating them.

Platform constraints that apply to the ingress: ports 80 and 443 only,
three-second response timeout, account 2FA required for webhooks, IPv4 only,
published source addresses 52.89.214.238, 34.212.75.30, 54.218.53.128,
52.32.178.7.

`htr_alert_adapter.pine` is an optional convenience: a modified copy, under MPL
2.0, that embeds the JSON in `message=` so the alert dialog needs no paste. The
original is preferable unless you want that.
