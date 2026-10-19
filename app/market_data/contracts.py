"""Provider-neutral market-data value types.

Every value that enters the engine from a data provider carries `Provenance`:
which provider produced it, when it was fetched, from which endpoint, and
whether it is mock data. A behavioural decision or a paper fill must never be
traceable only to "a number that was in memory", and mock data must never be
able to masquerade as a live quote.

Nothing here knows about Upstox, FYERS or Kite. Adapters translate into these
types; the engine reads only these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


@dataclass(frozen=True)
class Provenance:
    provider: str
    fetched_at: datetime
    endpoint: str | None = None
    is_mock: bool = False

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None:
            raise ValueError("Provenance.fetched_at must be timezone-aware UTC")


@dataclass(frozen=True)
class Instrument:
    """One tradable or quotable thing, as the engine refers to it.

    `provider_key` is the provider's own identifier (an Upstox `instrument_key`
    such as `NSE_FO|43309`, a FYERS symbol such as `NSE:NIFTY50-INDEX`). It is
    opaque to the engine: resolving it is the adapter's job, and the engine must
    never construct one by string-formatting, because strikes, expiries and lot
    sizes are facts to be looked up, not patterns to be guessed.
    """

    symbol: str
    provider_key: str
    provenance: Provenance
    exchange: str | None = None
    segment: str | None = None
    lot_size: int | None = None
    tick_size: float | None = None
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    underlying_symbol: str | None = None

    @property
    def is_option(self) -> bool:
        return self.option_type is not None


@dataclass(frozen=True)
class Tick:
    """A single price update. `exchange_time` is the exchange's timestamp."""

    symbol: str
    exchange_time: datetime
    last_price: float
    last_quantity: int | None = None
    cumulative_volume: int | None = None
    open_interest: int | None = None
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        if self.exchange_time.tzinfo is None:
            raise ValueError("Tick.exchange_time must be timezone-aware UTC")
        if not self.last_price > 0:
            raise ValueError(f"Tick.last_price must be positive, got {self.last_price}")


@dataclass(frozen=True)
class Candle:
    """A **completed** OHLC bar.

    There is no `is_complete` flag on purpose: a partial bar is not a Candle.
    `close_time` is the exclusive end of the interval, matching the engine's
    signal contract, so a candle and the signal on the same bar share a
    timestamp instead of being one bar apart.
    """

    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    open_interest: int | None = None
    tick_count: int | None = None
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        for name in ("open_time", "close_time"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"Candle.{name} must be timezone-aware UTC")
        if self.close_time <= self.open_time:
            raise ValueError("Candle.close_time must be after open_time")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(
                f"Candle OHLC is inconsistent: o={self.open} h={self.high} "
                f"l={self.low} c={self.close}"
            )

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)


@dataclass(frozen=True)
class Quote:
    """A two-sided quote. Options are bought at the ask and sold at the bid."""

    symbol: str
    quoted_at: datetime
    bid: float | None = None
    ask: float | None = None
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    last_price: float | None = None
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        if self.quoted_at.tzinfo is None:
            raise ValueError("Quote.quoted_at must be timezone-aware UTC")

    @property
    def is_two_sided(self) -> bool:
        return (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask > 0
            and self.ask >= self.bid
        )

    @property
    def spread(self) -> float | None:
        return None if not self.is_two_sided else self.ask - self.bid

    @property
    def mid(self) -> float | None:
        return None if not self.is_two_sided else (self.ask + self.bid) / 2

    @property
    def spread_fraction_of_mid(self) -> float | None:
        mid = self.mid
        return None if not mid else self.spread / mid

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        """A quote from the future is stale too: that is a clock or feed fault."""
        age = now - self.quoted_at
        return age > max_age or age < -max_age


@dataclass(frozen=True)
class CandleSeries:
    """Candles for one symbol and timeframe, ascending, with gaps left as gaps.

    Missing bars are **not** filled. A session break, a halt or a feed outage is
    a fact the behavioural engine must be able to see, and an interpolated
    candle is fabricated market data.
    """

    symbol: str
    timeframe: str
    candles: tuple[Candle, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        previous: Candle | None = None
        for candle in self.candles:
            if candle.symbol != self.symbol or candle.timeframe != self.timeframe:
                raise ValueError("CandleSeries holds one symbol and one timeframe only")
            if previous is not None and candle.open_time <= previous.open_time:
                raise ValueError("CandleSeries must be strictly ascending by open_time")
            previous = candle

    def __len__(self) -> int:
        return len(self.candles)

    def __iter__(self):
        return iter(self.candles)

    def gaps(self, bar: timedelta) -> tuple[tuple[datetime, datetime], ...]:
        """Intervals where bars are missing, as (after_close, next_open) pairs."""
        found: list[tuple[datetime, datetime]] = []
        for previous, current in zip(self.candles, self.candles[1:]):
            if current.open_time - previous.close_time >= bar:
                found.append((previous.close_time, current.open_time))
        return tuple(found)
