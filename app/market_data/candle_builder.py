"""Build completed OHLC candles from a tick stream.

Bar boundaries are anchored on the **session open in IST**, the same anchor the
signal contract uses (`app/signals/htr_adapter.py`). If candles were bucketed
from UTC midnight instead, a 10-minute candle built here would not line up with
the 10-minute bar TradingView fired its alert on, and every behavioural feature
computed against that signal would be reading the wrong bar.

What this class will not do:

* Emit a partial bar. A candle is returned only once its interval has provably
  ended -- either a tick arrived in a later bucket, or the caller asserted that
  time has passed via `close_through`.
* Invent an empty bar for a bucket that received no ticks. A gap is a fact.
* Accept a tick that belongs to an already-emitted bucket. Such a tick is
  counted as late and dropped, because rewriting a closed candle would change
  history a decision was already made on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.market_data.contracts import Candle, Provenance, Tick
from app.signals.timeframes import canonical_timeframe, timeframe_duration

IST = ZoneInfo("Asia/Kolkata")


class TickRejected(ValueError):
    """A tick that cannot be bucketed. Carries the counter name it was tallied under."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class BuilderStats:
    accepted: int = 0
    duplicates: int = 0
    late: int = 0
    before_session: int = 0
    candles_emitted: int = 0
    buckets_skipped: int = 0


@dataclass
class _OpenBucket:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0
    first_cumulative_volume: int | None = None
    last_cumulative_volume: int | None = None
    summed_quantity: int = 0
    has_quantities: bool = False
    last_open_interest: int | None = None
    seen: set[tuple] = field(default_factory=set)


class CandleBuilder:
    """Aggregates ticks for one symbol and one timeframe."""

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        session_open_ist: time,
        *,
        provider_name: str = "unknown",
        is_mock: bool = False,
    ) -> None:
        self.symbol = symbol
        self.timeframe = canonical_timeframe(timeframe)
        self._bar = timeframe_duration(self.timeframe)
        self._session_open = session_open_ist
        self._provider_name = provider_name
        self._is_mock = is_mock
        self._bucket: _OpenBucket | None = None
        self._last_emitted_close: datetime | None = None
        self.stats = BuilderStats()

    # --- bucketing --------------------------------------------------------

    def bucket_open_for(self, moment: datetime) -> datetime:
        """Opening instant of the bar containing `moment` (UTC in, UTC out)."""
        if moment.tzinfo is None:
            raise ValueError("bucketing requires a timezone-aware datetime")
        local = moment.astimezone(IST)
        anchor = local.replace(
            hour=self._session_open.hour,
            minute=self._session_open.minute,
            second=0,
            microsecond=0,
        )
        offset = local - anchor
        if offset < timedelta(0):
            raise TickRejected(
                f"{moment.isoformat()} is before the {self._session_open.isoformat()} "
                "IST session open",
                kind="before_session",
            )
        index = offset // self._bar
        return (anchor + index * self._bar).astimezone(moment.tzinfo)

    # --- ingestion --------------------------------------------------------

    def add(self, tick: Tick) -> Candle | None:
        """Feed one tick. Returns a candle only when one has just completed."""
        if tick.symbol != self.symbol:
            raise ValueError(f"tick for {tick.symbol!r} fed to a {self.symbol!r} builder")

        try:
            bucket_open = self.bucket_open_for(tick.exchange_time)
        except TickRejected as exc:
            self.stats.before_session += 1
            raise exc

        if self._last_emitted_close is not None and bucket_open < self._last_emitted_close:
            self.stats.late += 1
            return None

        completed: Candle | None = None
        if self._bucket is not None and bucket_open > self._bucket.open_time:
            completed = self._emit()
            # Buckets between the one just closed and this tick's bucket got no
            # ticks at all. They are counted, and deliberately not emitted.
            skipped = (bucket_open - self._last_emitted_close) // self._bar
            self.stats.buckets_skipped += max(0, int(skipped))

        if self._bucket is None or bucket_open > self._bucket.open_time:
            self._bucket = _OpenBucket(
                open_time=bucket_open,
                close_time=bucket_open + self._bar,
                open=tick.last_price,
                high=tick.last_price,
                low=tick.last_price,
                close=tick.last_price,
            )

        bucket = self._bucket
        fingerprint = (tick.exchange_time, tick.last_price, tick.cumulative_volume)
        if fingerprint in bucket.seen:
            self.stats.duplicates += 1
            return completed
        bucket.seen.add(fingerprint)

        bucket.high = max(bucket.high, tick.last_price)
        bucket.low = min(bucket.low, tick.last_price)
        bucket.close = tick.last_price
        bucket.tick_count += 1
        if tick.cumulative_volume is not None:
            if bucket.first_cumulative_volume is None:
                bucket.first_cumulative_volume = tick.cumulative_volume
            bucket.last_cumulative_volume = tick.cumulative_volume
        if tick.last_quantity is not None:
            bucket.has_quantities = True
            bucket.summed_quantity += tick.last_quantity
        if tick.open_interest is not None:
            bucket.last_open_interest = tick.open_interest
        self.stats.accepted += 1
        return completed

    def close_through(self, moment: datetime) -> list[Candle]:
        """Emit the open bar if `moment` proves its interval has ended.

        The caller asserts that time has advanced -- from a heartbeat, a clock,
        or the end of a replay. Nothing is emitted on the strength of a guess.
        """
        if self._bucket is None or moment < self._bucket.close_time:
            return []
        return [self._emit()]

    def _emit(self) -> Candle:
        bucket = self._bucket
        assert bucket is not None
        volume: int | None = None
        if (
            bucket.first_cumulative_volume is not None
            and bucket.last_cumulative_volume is not None
        ):
            volume = max(0, bucket.last_cumulative_volume - bucket.first_cumulative_volume)
        elif bucket.has_quantities:
            volume = bucket.summed_quantity

        candle = Candle(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=bucket.open_time,
            close_time=bucket.close_time,
            open=bucket.open,
            high=bucket.high,
            low=bucket.low,
            close=bucket.close,
            volume=volume,
            open_interest=bucket.last_open_interest,
            tick_count=bucket.tick_count,
            provenance=Provenance(
                provider=self._provider_name,
                fetched_at=bucket.close_time,
                endpoint="candle_builder",
                is_mock=self._is_mock,
            ),
        )
        self._bucket = None
        self._last_emitted_close = candle.close_time
        self.stats.candles_emitted += 1
        return candle

    @property
    def open_bar_close_time(self) -> datetime | None:
        """When the bar currently being built will close, if one is open."""
        return None if self._bucket is None else self._bucket.close_time
