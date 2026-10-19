"""Upstox adapter -- NOT IMPLEMENTED.

Primary live-data candidate for M3. This module exists to hold the facts
gathered from Upstox's official v3 documentation on 2026-10-19 so that the
implementation starts from documentation rather than from assumption, and to
fail loudly instead of returning anything that could be mistaken for a quote.

Documented facts (read from the official v3 historical-candle page; everything
marked "unverified" was not confirmed against a live account):

* Historical candles:
  `GET https://api.upstox.com/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}`
  - `to_date` is required and precedes the optional `from_date` in the path.
  - Dates are `YYYY-MM-DD`. Header: `Authorization: Bearer {access_token}`.
  - Units/intervals: `minutes` 1-300, `hours` 1-5, `days`/`weeks`/`months` 1 only.
  - History depth: minutes and hours from January 2022; days, weeks and months
    from January 2000.
  - Max range per request: 1 month for 1-15 minute intervals, 1 quarter above
    15 minutes, 1 quarter for hours, 1 decade for days. Exceeding the record
    limit returns an invalid-request error, so the adapter must page.
  - Each candle is an array: `[timestamp, open, high, low, close, volume,
    open_interest]`, timestamp being the candle's **start** time. Samples are
    ISO 8601 with a `+05:30` offset; the page does not state the timezone
    explicitly, so the offset is the only evidence and must be re-checked
    against live responses before being relied on.
* Instrument keys look like `SEGMENT|token` or `SEGMENT|ISIN`
  (`NSE_EQ|INE848E01016`, `NSE_INDEX|India VIX`, `NSE_FO|43309`). The pipe is
  URL-encoded as `%7C`. Errors: `UDAPI1021` invalid key format,
  `UDAPI100011` key not recognised.
* Instrument master is a CDN download (no access token needed), JSON preferred
  over the deprecated CSV; the complete file is reported at
  `assets.upstox.com/market-quote/instruments/exchange/complete.json.gz`.
  Records carry `instrument_key`, `trading_symbol`, `exchange_token`,
  `lot_size`, `tick_size`, and for derivatives `expiry` (millisecond epoch) and
  `strike_price`. **Unverified**: the exact field list and the CSV-to-JSON
  mapping were not read from the official instruments page (404 at the URL
  tried), and duplicate keys have been reported for some BSE segments.
* Market Feed V3 is the websocket. OHLC in the feed covers limited intervals
  only (1-minute and 1-day; FULL mode is reported to include 30-minute), which
  is why this engine builds its own candles from ticks
  (`app.market_data.candle_builder`) instead of trusting feed-side bars.
* Rate limits were **not** stated on the historical-candle page; they live on a
  separate page that must be read before any polling loop is written.

Open questions that must be answered with a live account before M3 can pass:
expiry-day behaviour of the instrument master, whether index option quotes
expose depth, and whether expired-contract history is retrievable at all.
"""

from __future__ import annotations

from datetime import date, datetime

from app.market_data.contracts import CandleSeries, Instrument, OptionType, Quote
from app.market_data.protocols import CredentialsMissing

PROVIDER_NAME = "upstox"

HISTORICAL_CANDLE_PATH = (
    "/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
)
INSTRUMENTS_COMPLETE_JSON_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)

# Documented maximum request span in days, by interval unit. Used by the future
# implementation to page a request instead of silently truncating it.
MAX_REQUEST_DAYS = {
    "minutes_upto_15": 31,
    "minutes_above_15": 92,
    "hours": 92,
    "days": 3653,
}
EARLIEST_INTRADAY = date(2022, 1, 1)
EARLIEST_DAILY = date(2000, 1, 1)


def _unavailable(what: str):
    raise CredentialsMissing(
        f"Upstox {what} is not implemented. M3 needs an API key, an access-token "
        "flow and the official rate-limit page. Until then this provider returns "
        "nothing rather than anything resembling market data."
    )


class UpstoxInstrumentMaster:
    provider_name = PROVIDER_NAME

    def refresh(self) -> datetime:
        _unavailable("instrument master")

    def resolve(self, symbol: str) -> Instrument:
        _unavailable("symbol resolution")

    def index_instrument(self, symbol: str) -> Instrument:
        _unavailable("index instrument lookup")

    def option_expiries(self, underlying_symbol: str) -> tuple[date, ...]:
        _unavailable("expiry listing")

    def option_contracts(
        self,
        underlying_symbol: str,
        expiry: date,
        option_type: OptionType | None = None,
    ) -> tuple[Instrument, ...]:
        _unavailable("option contract listing")


class UpstoxHistoricalCandles:
    provider_name = PROVIDER_NAME

    def max_range(self, timeframe: str) -> int:
        _unavailable("historical candles")

    def earliest_available(self, timeframe: str) -> date | None:
        _unavailable("historical candles")

    def fetch_candles(
        self, instrument: Instrument, timeframe: str, start: datetime, end: datetime
    ) -> CandleSeries:
        _unavailable("historical candles")


class UpstoxQuotes:
    provider_name = PROVIDER_NAME

    def quote(self, instrument: Instrument) -> Quote:
        _unavailable("quotes")

    def quotes(self, instruments: tuple[Instrument, ...]) -> dict[str, Quote]:
        _unavailable("quotes")
