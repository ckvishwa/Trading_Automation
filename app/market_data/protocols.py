"""Interfaces every market-data provider must satisfy.

These are `typing.Protocol`s, so a provider adapter, a recorded-data replay
source and an offline mock are interchangeable without inheritance. The engine
depends on these names only; swapping Upstox for FYERS, or either for recorded
fixtures, must not reach into the behavioural engine.

Rules a conforming adapter must honour:

* Return the value types in `app.market_data.contracts`, with `Provenance`
  filled in, timestamps timezone-aware UTC.
* Raise rather than invent. A missing candle, a one-sided option quote and an
  unavailable expired contract are all errors or empty results, never
  interpolated or synthesised values.
* Never widen a request silently. If the provider caps a date range, the
  adapter either pages the request or raises; it does not quietly return less
  than was asked for.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import AsyncIterator, Protocol, runtime_checkable

from app.market_data.contracts import (
    Candle,
    CandleSeries,
    Instrument,
    OptionType,
    Quote,
    Tick,
)


class MarketDataError(RuntimeError):
    """Base class for provider failures. Never swallowed, never defaulted away."""


class DataUnavailable(MarketDataError):
    """The provider has no data for this request.

    Raised instead of returning fabricated values -- notably for expired option
    contracts, whose historical quotes are frequently unavailable and must not
    be reconstructed from the underlying's move.
    """


class CredentialsMissing(MarketDataError):
    """No usable credentials, so no live claim can be made."""


class RateLimited(MarketDataError):
    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class FeedDisconnected(MarketDataError):
    """The stream dropped. Callers reconnect with backoff; they do not carry on."""


@runtime_checkable
class InstrumentMaster(Protocol):
    """Resolution of engine symbols to provider instruments.

    Lot sizes, expiries, strikes and tick sizes come from here. The engine never
    hardcodes or derives them.
    """

    provider_name: str

    def refresh(self) -> datetime:
        """Load or reload the instrument master; returns when it was fetched."""

    def resolve(self, symbol: str) -> Instrument:
        """Resolve one engine symbol, e.g. `NSE:NIFTY`. Raises DataUnavailable."""

    def index_instrument(self, symbol: str) -> Instrument:
        """The index instrument itself. Must be identity-checked before use."""

    def option_expiries(self, underlying_symbol: str) -> tuple[date, ...]:
        """Available expiries, ascending. Never a generated weekly pattern."""

    def option_contracts(
        self,
        underlying_symbol: str,
        expiry: date,
        option_type: OptionType | None = None,
    ) -> tuple[Instrument, ...]:
        """Real listed contracts for an expiry, with their real lot sizes."""


@runtime_checkable
class HistoricalCandleSource(Protocol):
    """Completed historical candles for research and replay."""

    provider_name: str

    def max_range(self, timeframe: str) -> int:
        """Maximum days of history the provider allows in one request."""

    def earliest_available(self, timeframe: str) -> date | None:
        """Start of the provider's history for this timeframe, if documented."""

    def fetch_candles(
        self, instrument: Instrument, timeframe: str, start: datetime, end: datetime
    ) -> CandleSeries:
        """Candles with `start <= open_time` and `close_time <= end`.

        Partial trailing bars are excluded: only completed candles are returned.
        """


@runtime_checkable
class LiveTickFeed(Protocol):
    """A streaming feed of ticks."""

    provider_name: str

    async def subscribe(self, instruments: tuple[Instrument, ...]) -> None: ...

    async def unsubscribe(self, instruments: tuple[Instrument, ...]) -> None: ...

    def stream(self) -> AsyncIterator[Tick]:
        """Ticks as they arrive. Raises FeedDisconnected when the stream drops."""

    @property
    def last_message_at(self) -> datetime | None:
        """Used by the watchdog to detect a silent-but-open connection."""


@runtime_checkable
class QuoteSource(Protocol):
    """Point-in-time two-sided quotes, for option selection and paper fills."""

    provider_name: str

    def quote(self, instrument: Instrument) -> Quote: ...

    def quotes(self, instruments: tuple[Instrument, ...]) -> dict[str, Quote]: ...


@runtime_checkable
class CandleSink(Protocol):
    """Where completed candles are handed on to -- storage, or the engine."""

    def accept(self, candle: Candle) -> None: ...
