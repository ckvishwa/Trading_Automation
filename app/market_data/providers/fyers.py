"""FYERS adapter -- NOT IMPLEMENTED.

Intended role: **historical research data** (chronological replay, walk-forward
evaluation), with Upstox as the primary live feed. Keeping the two roles in
separate adapters means a research-only outage cannot affect the live path, and
a disagreement between the two is detectable rather than invisible.

Documented facts (FYERS API v3 historical-data documentation, read 2026-10-19;
community-reported items are labelled as such):

* Resolutions and per-request caps:
  - Intraday 1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120, 180, 240 minutes:
    up to **100 days** of data per request.
  - 1D, 1W, 1M: up to **366 days** per request.
* `date_format` 0 means `range_from`/`range_to` are epoch values; 1 means
  `yyyy-mm-dd`.
* The docs advise setting `range_to` to the **previous minute** so only
  completed candles are returned -- the same partial-bar hazard this engine
  guards against on the signal side.
* The app needs the Market Data permission (historical data, market depth,
  quotes). A 403 has been traced to app permissions rather than the token.
* Community-reported, treat as a hazard to test for, not as documented
  behaviour: date-only strings on intraday requests have returned far less than
  the requested range (in one report only the final day), and moderators advised
  passing epoch values including time. Error -300 "Please provide a valid
  symbol" is common when the symbol format is wrong.
* **Unverified**: the symbol/instrument master URL and its field list were not
  located from the official docs in this session, nor was the rate-limit table,
  nor whether expired option contracts have retrievable history.

Consequences already designed for elsewhere in the engine: expired option price
history may simply not exist, so `app/research/` must keep underlying
directional evaluation separate from option-strategy P&L rather than treating
index points as option profit.
"""

from __future__ import annotations

from datetime import date, datetime

from app.market_data.contracts import CandleSeries, Instrument
from app.market_data.protocols import CredentialsMissing

PROVIDER_NAME = "fyers"

# Documented per-request caps, in days, keyed by canonical timeframe family.
MAX_REQUEST_DAYS_INTRADAY = 100
MAX_REQUEST_DAYS_DAILY_PLUS = 366
INTRADAY_RESOLUTION_MINUTES = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120, 180, 240)


def _unavailable(what: str):
    raise CredentialsMissing(
        f"FYERS {what} is not implemented. M3 needs app credentials with the "
        "Market Data permission, plus the official symbol-master and rate-limit "
        "references. Until then this provider returns nothing."
    )


class FyersHistoricalCandles:
    provider_name = PROVIDER_NAME

    def max_range(self, timeframe: str) -> int:
        _unavailable("historical candles")

    def earliest_available(self, timeframe: str) -> date | None:
        _unavailable("historical candles")

    def fetch_candles(
        self, instrument: Instrument, timeframe: str, start: datetime, end: datetime
    ) -> CandleSeries:
        _unavailable("historical candles")
