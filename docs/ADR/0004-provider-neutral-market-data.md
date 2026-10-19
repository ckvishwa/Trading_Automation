# ADR 0004 — Provider-neutral market data; Upstox live, FYERS research

* Status: Accepted
* Date: 2026-10-19
* Milestone: M3 (architecture agreed; no adapter implemented)

## Context

M3 needs live index data, completed candles, option contracts and option quotes.
The original plan named Kite Connect. The owner has since designated **Upstox**
as the primary live-data candidate and **FYERS** for historical research.

Indian broker data APIs differ in ways that reach into application code if the
engine talks to them directly: instrument identifiers (`NSE_FO|43309` versus a
`NSE:NIFTY...CE` symbol string), candle payload shapes, per-request range caps,
history depth, resolution vocabularies, and what is simply absent — notably
historical quotes for expired option contracts.

## Decision

The engine depends only on `app/market_data/protocols.py` and the value types in
`app/market_data/contracts.py`. Adapters translate; nothing above the adapter
layer knows a provider's name.

1. **Two roles, two adapters.** `providers/upstox.py` for the live path,
   `providers/fyers.py` for research history. A research-data outage cannot
   affect the live path, and a disagreement between them is detectable because
   each value carries its own `Provenance`.
2. **Provenance on every value.** Provider, fetch time, endpoint, and an
   explicit `is_mock` flag. Mock data cannot be mistaken for a live quote, and a
   later risk or execution stage can refuse to act on mock provenance.
3. **Candles are built locally** from ticks (`candle_builder.py`), anchored on
   the IST session open, not taken from provider-side OHLC. Upstox's feed
   publishes only a few fixed OHLC intervals, and a provider's bar grid is not
   guaranteed to match the grid TradingView fired the alert on. Same anchor as
   the signal contract, or the signal and its candle describe different bars.
4. **Raise rather than invent.** `DataUnavailable` for missing data,
   `CredentialsMissing` when there is nothing to authenticate with,
   `FeedDisconnected` when a stream drops. Gaps in a `CandleSeries` stay gaps.
5. **Fail closed on feed health.** `FeedWatchdog.is_usable` is true only for an
   explicitly healthy feed; an open-but-silent socket reports `SILENT`.
6. **Identifiers are looked up, never constructed.** Lot sizes, expiries,
   strikes and tick sizes come from an instrument master. The engine never
   string-formats an option symbol.

## Documented provider facts behind the interface

Read from official documentation on 2026-10-19 and recorded in each adapter's
docstring. Everything here is doc-level; none of it has been confirmed against a
live account.

**Upstox v3**:
`GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}`;
units `minutes` 1–300, `hours` 1–5, `days`/`weeks`/`months` 1 only; intraday
history from January 2022, daily from January 2000; max one month per request
for 1–15 minute intervals, one quarter above that, one decade for days; candle
arrays are `[timestamp, open, high, low, close, volume, open_interest]` with the
timestamp being the bar's **start**, sampled as ISO 8601 `+05:30`; instrument
keys are `SEGMENT|token`; instrument master is a CDN JSON download (CSV
deprecated). Rate limits are on a separate page that must be read before any
polling loop is written.

**FYERS v3**: intraday resolutions 1–240 minutes capped at **100 days** per
request, 1D/1W/1M capped at **366 days**; `date_format` 0 for epoch, 1 for
`yyyy-mm-dd`; the docs advise a `range_to` of the previous minute so only
completed candles are returned. Community reports of short responses for
date-only intraday ranges are recorded as a hazard to test for.

## Consequences

* Swapping or adding a provider is an adapter change plus a protocol conformance
  test. The behavioural engine is untouched.
* Both adapters currently raise `CredentialsMissing`. The system is honest about
  having no market data rather than appearing to work.
* Paging logic is each adapter's responsibility, because the caps differ.
* Expired-option history may not exist at either provider. M7 must therefore
  keep underlying directional evaluation separate from option-strategy P&L, and
  must never present index points as option profit.
* Kite Connect is not removed from consideration; it is simply not the current
  candidate. Nothing in the engine depends on that choice.
