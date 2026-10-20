# M1 — capturing genuine HTR alerts and running parity

M1 is PARTIAL and stays PARTIAL until genuine TradingView alerts are captured
and matched against the Python reference. This is the procedure. Nothing here
changes the indicator; TradingView remains the sole signal generator.

## 1. The exact alert configuration

Use the original AlgoAlpha indicator unchanged
(`pine/half_trend_regression_algoalpha.pine`). Two alerts per chart:

| Alert | Condition | Engine signal | Message file |
|---|---|---|---|
| Bullish Trend Alert | `ta.crossover(trendDirection, 0)` | BUY | `examples/htr-buy-alert.json` |
| Bearish Trend Alert | `ta.crossunder(trendDirection, 0)` | SELL | `examples/htr-sell-alert.json` |

For each:

1. Open the saved **NSE:NIFTY** chart with your tuned HTR inputs (defaults
   amplitude 2, channel deviation 2.0, regression length 7).
2. Create alert → Condition: **AlgoAlpha - Half Trend** → the trend alert above.
3. Frequency: **Once Per Bar Close**. Not optional — the forming bar can flicker
   (see `pine/HTR_ANALYSIS.md §5`), and the engine rejects any alert that fired
   before its bar closed.
4. Message: paste the matching JSON file verbatim.
5. Webhook URL: `https://YOUR_HTTPS_HOST/v1/webhooks/tradingview/YOUR_WEBHOOK_TOKEN`.

Do **not** use the Universal Trend Alert: it fires both directions and the
payload cannot say which.

## 2. Capture

Run the engine behind your HTTPS ingress. Each genuine alert is recorded in the
journal with full transport telemetry, and `GET /v1/telemetry/signals` shows
`bar_open_time`, `bar_close_time`, `fired_at`, `received_at`, `delivery_delay_ms`,
`bar_close_lag_ms`, `freshness`, `tradable` and `signal`.

Collect signals across several sessions covering both directions. Export them as
JSON lines to `data/reference/htr_genuine_alerts.jsonl`, one per genuine alert:

```json
{"bar_close_time": "2026-10-21T04:40:00Z", "signal": "SELL", "source": "tradingview", "price": 24010.5}
```

`source` **must** be `tradingview`. `app/research/htr_parity.py` rejects any
other provenance, so a fixture cannot be passed off as evidence.

## 3. Candles for the same window

Fetch completed 5-minute NIFTY candles covering the alert window from a verified
market-data source (Upstox historical, once credentialed — see
`tools/upstox_live_smoke.py` and the `v3/historical-candle` path). Start the
candle series where the chart's history starts: the indicator's **first** flip
depends on loaded history (`pine/HTR_ANALYSIS.md §3`), though every signal after
both runs have flipped agrees.

## 4. Run parity

```python
from app.research.htr_parity import load_recorded_alerts, run_parity
# candles: a CandleSeries for the same symbol/timeframe/window
alerts = load_recorded_alerts(path_to_genuine_alerts)
result = run_parity(candles, alerts)
print(result.as_dict())
```

`result.parity` is True only when every genuine alert matches the reference on
both bar-close time and direction, over a non-empty overlap. It is withheld
whenever any record is non-genuine. The `live`-marked test
`tests/replay/test_htr_parity.py::test_parity_against_real_tradingview_alerts_is_not_yet_possible`
becomes the standing check once the file exists.

## 5. What closes M1

* Genuine alerts captured end to end (not synthetic, not replayed).
* Candles for the same window from a verified source.
* `run_parity` reports `parity=True` with `genuine_evidence=True`.
* At least one live alert observed through the real HTTPS ingress, confirming
  the timestamp and direction mapping against something other than a synthetic
  payload.

Until all four hold, M1 is PARTIAL. The engine already handles the transport,
the contract, the telemetry and the reference calculation; what is missing is
genuine data, which only the owner's live chart can produce.
