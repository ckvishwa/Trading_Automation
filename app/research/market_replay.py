"""Deterministic replay of a recorded market-data feed, end to end.

The route is the live one, in the live order:

    Protobuf frame -> decoder -> normalized tick -> candle builder
                   -> SQLite storage -> feed health state

Nothing is simulated except the transport. The decoder, normalizer, builder,
store and watchdog are the same objects the live path uses, so a replay cannot
pass while the live path would fail on the same bytes.

Determinism rules:

* The clock comes from the frames. `currentTs`, or failing that the tick's own
  exchange time, drives the watchdog and the bar boundaries. Wall time is never
  read, so a replay produces the same result tomorrow.
* Frames are processed in file order. They are not sorted: the recording's order
  *is* the fact under test, and a late or duplicate frame must be handled rather
  than tidied away.
* Nothing is fabricated. A bucket with no ticks yields no candle, a malformed
  frame is counted and skipped rather than interpolated, and a bar already in
  the store is left as it was.

Frames are stored length-prefixed (4-byte big-endian) because Protobuf messages
are not self-delimiting: concatenating two of them yields a third valid message
with later fields silently overwriting earlier ones.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from app.market_data.candle_builder import CandleBuilder, TickRejected
from app.market_data.contracts import Candle, Quote, Tick
from app.market_data.health import FeedState, FeedWatchdog
from app.market_data.protocols import MarketDataError
from app.market_data.providers.upstox import (
    ProtobufFeedDecoder,
    UpstoxFeedNormalizer,
)
from app.market_data.session import Segment, SessionCalendar
from app.storage.event_store import EventStore

LENGTH_PREFIX = struct.Struct(">I")


def write_length_prefixed(path: Path, frames: Iterable[bytes]) -> int:
    written = 0
    with Path(path).open("wb") as handle:
        for frame in frames:
            handle.write(LENGTH_PREFIX.pack(len(frame)))
            handle.write(frame)
            written += 1
    return written


def read_length_prefixed(path: Path) -> Iterator[bytes]:
    """Yield each frame, failing on a truncated file rather than guessing."""
    data = Path(path).read_bytes()
    offset = 0
    while offset < len(data):
        if offset + LENGTH_PREFIX.size > len(data):
            raise ValueError(
                f"{path}: truncated length prefix at byte {offset}; the recording "
                "is incomplete and will not be partially replayed"
            )
        (size,) = LENGTH_PREFIX.unpack_from(data, offset)
        offset += LENGTH_PREFIX.size
        if offset + size > len(data):
            raise ValueError(
                f"{path}: frame at byte {offset} claims {size} bytes but only "
                f"{len(data) - offset} remain"
            )
        yield data[offset : offset + size]
        offset += size


@dataclass
class MarketReplaySummary:
    frames: int = 0
    decoded: int = 0
    malformed: int = 0
    status_messages: int = 0
    ticks: int = 0
    quotes: int = 0
    rejected_updates: int = 0
    candles_built: int = 0
    candles_stored: int = 0
    candles_duplicate: int = 0
    ticks_before_session: int = 0
    reasons: Counter = field(default_factory=Counter)
    segment_status: dict[str, str] = field(default_factory=dict)
    final_feed_state: str | None = None
    first_message_time: datetime | None = None
    last_message_time: datetime | None = None

    def as_dict(self) -> dict:
        """A stable, comparable shape: two identical replays compare equal."""
        return {
            "frames": self.frames,
            "decoded": self.decoded,
            "malformed": self.malformed,
            "status_messages": self.status_messages,
            "ticks": self.ticks,
            "quotes": self.quotes,
            "rejected_updates": self.rejected_updates,
            "candles_built": self.candles_built,
            "candles_stored": self.candles_stored,
            "candles_duplicate": self.candles_duplicate,
            "ticks_before_session": self.ticks_before_session,
            "reasons": dict(sorted(self.reasons.items())),
            "segment_status": dict(sorted(self.segment_status.items())),
            "final_feed_state": self.final_feed_state,
        }


class MarketDataReplay:
    """Replays recorded feed frames through the production market-data path."""

    def __init__(
        self,
        store: EventStore,
        *,
        symbol: str,
        timeframe: str,
        session_open_ist: time,
        decoder: ProtobufFeedDecoder | None = None,
        normalizer: UpstoxFeedNormalizer | None = None,
        provider_name: str = "upstox",
        is_mock: bool = True,
        silence_limit: timedelta = timedelta(seconds=30),
        strict: bool = False,
    ) -> None:
        self._store = store
        self._symbol = symbol
        self._timeframe = timeframe
        self._decoder = decoder or ProtobufFeedDecoder()
        self._normalizer = normalizer or UpstoxFeedNormalizer(
            key_to_symbol=lambda key: symbol
        )
        self._builder = CandleBuilder(
            symbol,
            timeframe,
            session_open_ist,
            provider_name=provider_name,
            is_mock=is_mock,
        )
        self._watchdog = FeedWatchdog(silence_limit=silence_limit)
        # strict=True re-raises a bad frame instead of counting it. A research
        # replay wants the count; a conformance check wants the failure.
        self._strict = strict
        self.candles: list[Candle] = []
        self.quotes: list[Quote] = []
        self.ticks: list[Tick] = []

    @classmethod
    def for_calendar(
        cls,
        store: EventStore,
        calendar: SessionCalendar,
        *,
        symbol: str,
        timeframe: str,
        on: datetime,
        segment: Segment = Segment.FO,
        **kwargs,
    ) -> "MarketDataReplay":
        """Build a replay whose bar grid is anchored on the exchange calendar.

        Same anchor as the signal contract, so a candle and the alert fired on
        that bar describe the same interval.
        """
        return cls(
            store,
            symbol=symbol,
            timeframe=timeframe,
            session_open_ist=calendar.segment_open(segment, on=on.date()),
            **kwargs,
        )

    @property
    def feed_state(self) -> FeedState:
        moment = self._watchdog.last_message_at
        return self._watchdog.state(moment or datetime.min)

    @property
    def stats(self):
        return self._builder.stats

    def run(self, frames: Sequence[bytes]) -> MarketReplaySummary:
        summary = MarketReplaySummary(frames=len(frames))
        connected_at: datetime | None = None

        for frame in frames:
            try:
                message = self._decoder.decode(frame)
            except MarketDataError as exc:
                if self._strict:
                    raise
                summary.malformed += 1
                summary.reasons["MALFORMED_FRAME"] += 1
                del exc
                continue
            summary.decoded += 1

            message_time = (
                datetime.fromtimestamp(
                    int(message["currentTs"]) / 1000, tz=self._utc()
                )
                if message.get("currentTs")
                else None
            )
            if message_time is not None:
                if connected_at is None:
                    connected_at = message_time
                    self._watchdog.on_connect(message_time)
                self._watchdog.on_message(message_time)
                summary.first_message_time = summary.first_message_time or message_time
                summary.last_message_time = message_time

            status = (message.get("marketInfo") or {}).get("segmentStatus") or {}
            if status:
                summary.status_messages += 1
                summary.segment_status.update(
                    {str(key): str(value) for key, value in status.items()}
                )
                # A status frame carries no prices; nothing else to do with it.
                continue

            try:
                updates = self._normalizer.normalize_message(message)
            except MarketDataError as exc:
                if self._strict:
                    raise
                summary.rejected_updates += 1
                summary.reasons[self._reason_of(exc)] += 1
                continue

            for update in updates:
                if update.quote is not None:
                    self.quotes.append(update.quote)
                    summary.quotes += 1
                if update.tick is None:
                    continue
                self.ticks.append(update.tick)
                summary.ticks += 1
                try:
                    completed = self._builder.add(update.tick)
                except TickRejected:
                    summary.ticks_before_session += 1
                    summary.reasons["TICK_BEFORE_SESSION"] += 1
                    continue
                if completed is not None:
                    self._record(completed, summary)

        # Close the final bar only if the recording proves its interval ended.
        if summary.last_message_time is not None:
            for completed in self._builder.close_through(summary.last_message_time):
                self._record(completed, summary)

        summary.final_feed_state = self.feed_state.value
        return summary

    def run_file(self, path: Path) -> MarketReplaySummary:
        return self.run(list(read_length_prefixed(path)))

    def _record(self, candle: Candle, summary: MarketReplaySummary) -> None:
        self.candles.append(candle)
        summary.candles_built += 1
        if self._store.store_candle(candle, stored_at=candle.close_time):
            summary.candles_stored += 1
        else:
            summary.candles_duplicate += 1

    @staticmethod
    def _reason_of(error: MarketDataError) -> str:
        text = str(error)
        if "crossed book" in text:
            return "CROSSED_BOOK"
        if "tolerance" in text:
            return "QUOTE_TIMESTAMP_MISMATCH"
        if "no 'ltp'" in text or "ltp" in text:
            return "BAD_LAST_PRICE"
        if "carries no ltpc" in text:
            return "EMPTY_FEED"
        if "no 'feeds'" in text:
            return "NO_FEEDS"
        return "NORMALIZATION_ERROR"

    @staticmethod
    def _utc():
        from datetime import timezone

        return timezone.utc
