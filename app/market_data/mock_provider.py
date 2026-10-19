"""Offline, deterministic market data for tests and development.

Every value it returns is stamped `is_mock=True`, so a mock price cannot be
mistaken downstream for a live one, and a risk or execution stage can refuse to
act on mock provenance. It serves only what it was given: an unknown symbol,
timeframe or expiry raises `DataUnavailable` rather than producing a plausible
number, because a provider that invents data is worse than one that is absent.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.market_data.contracts import (
    Candle,
    CandleSeries,
    Instrument,
    OptionType,
    Provenance,
    Quote,
)
from app.market_data.protocols import DataUnavailable

PROVIDER_NAME = "mock"


def mock_provenance(
    fetched_at: datetime | None = None, endpoint: str = "fixture"
) -> Provenance:
    return Provenance(
        provider=PROVIDER_NAME,
        fetched_at=fetched_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        endpoint=endpoint,
        is_mock=True,
    )


class RecordedMarketData:
    """A provider backed by explicitly supplied series, instruments and quotes."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        instruments: dict[str, Instrument] | None = None,
        series: dict[tuple[str, str], CandleSeries] | None = None,
        quotes: dict[str, Quote] | None = None,
    ) -> None:
        self._instruments = dict(instruments or {})
        self._series = dict(series or {})
        self._quotes = dict(quotes or {})
        self._refreshed_at: datetime | None = None

    # --- InstrumentMaster -------------------------------------------------

    def refresh(self) -> datetime:
        self._refreshed_at = datetime.now(tz=timezone.utc)
        return self._refreshed_at

    def resolve(self, symbol: str) -> Instrument:
        try:
            return self._instruments[symbol]
        except KeyError:
            raise DataUnavailable(f"mock provider holds no instrument for {symbol!r}") from None

    def index_instrument(self, symbol: str) -> Instrument:
        instrument = self.resolve(symbol)
        if instrument.is_option:
            raise DataUnavailable(f"{symbol!r} resolves to an option, not an index")
        return instrument

    def option_expiries(self, underlying_symbol: str) -> tuple[date, ...]:
        expiries = {
            instrument.expiry
            for instrument in self._instruments.values()
            if instrument.underlying_symbol == underlying_symbol and instrument.expiry
        }
        if not expiries:
            raise DataUnavailable(f"no option contracts recorded for {underlying_symbol!r}")
        return tuple(sorted(expiries))

    def option_contracts(
        self,
        underlying_symbol: str,
        expiry: date,
        option_type: OptionType | None = None,
    ) -> tuple[Instrument, ...]:
        found = tuple(
            instrument
            for instrument in self._instruments.values()
            if instrument.underlying_symbol == underlying_symbol
            and instrument.expiry == expiry
            and (option_type is None or instrument.option_type is option_type)
        )
        if not found:
            raise DataUnavailable(
                f"no {option_type or 'CE/PE'} contracts recorded for "
                f"{underlying_symbol!r} expiring {expiry.isoformat()}"
            )
        return tuple(sorted(found, key=lambda i: (i.strike or 0.0, i.option_type or "")))

    # --- HistoricalCandleSource -------------------------------------------

    def max_range(self, timeframe: str) -> int:
        return 10_000

    def earliest_available(self, timeframe: str) -> date | None:
        earliest = [
            series.candles[0].open_time.date()
            for (_, series_timeframe), series in self._series.items()
            if series_timeframe == timeframe and series.candles
        ]
        return min(earliest) if earliest else None

    def fetch_candles(
        self, instrument: Instrument, timeframe: str, start: datetime, end: datetime
    ) -> CandleSeries:
        key = (instrument.symbol, timeframe)
        if key not in self._series:
            raise DataUnavailable(
                f"mock provider holds no {timeframe} series for {instrument.symbol!r}"
            )
        source = self._series[key]
        selected = tuple(
            candle
            for candle in source.candles
            if candle.open_time >= start and candle.close_time <= end
        )
        return CandleSeries(symbol=instrument.symbol, timeframe=timeframe, candles=selected)

    # --- QuoteSource ------------------------------------------------------

    def quote(self, instrument: Instrument) -> Quote:
        try:
            return self._quotes[instrument.symbol]
        except KeyError:
            raise DataUnavailable(
                f"mock provider holds no quote for {instrument.symbol!r}"
            ) from None

    def quotes(self, instruments: tuple[Instrument, ...]) -> dict[str, Quote]:
        return {instrument.symbol: self.quote(instrument) for instrument in instruments}

    # --- test helpers -----------------------------------------------------

    def add_instrument(self, instrument: Instrument) -> None:
        self._instruments[instrument.symbol] = instrument

    def add_series(self, series: CandleSeries) -> None:
        self._series[(series.symbol, series.timeframe)] = series

    def add_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol] = quote


def index_instrument(symbol: str = "NSE:NIFTY", provider_key: str = "MOCK|NIFTY") -> Instrument:
    return Instrument(
        symbol=symbol,
        provider_key=provider_key,
        exchange="NSE",
        segment="INDEX",
        provenance=mock_provenance(),
    )


def option_instrument(
    *,
    underlying_symbol: str = "NSE:NIFTY",
    expiry: date,
    strike: float,
    option_type: OptionType,
    lot_size: int = 75,
    tick_size: float = 0.05,
) -> Instrument:
    """A mock option contract.

    `lot_size` is a parameter with no authoritative default: the real value must
    come from a provider's instrument master, and 75 here is a fixture value,
    not a claim about the current NIFTY lot size.
    """
    symbol = (
        f"{underlying_symbol}-{expiry.isoformat()}-{strike:g}-{option_type.value}"
    )
    return Instrument(
        symbol=symbol,
        provider_key=f"MOCK|{symbol}",
        exchange="NSE",
        segment="FO",
        lot_size=lot_size,
        tick_size=tick_size,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        underlying_symbol=underlying_symbol,
        provenance=mock_provenance(),
    )


def candle(
    *,
    symbol: str,
    timeframe: str,
    open_time: datetime,
    close_time: datetime,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: int | None = None,
) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        close_time=close_time,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        provenance=mock_provenance(endpoint="fixture_candle"),
    )
