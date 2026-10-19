"""End-to-end market-data replay.

    Protobuf wire bytes -> decoder -> normalized tick -> candle builder
                        -> SQLite storage -> feed health state

The frames are genuine Protobuf, serialized from Upstox's official schema, so
this exercises the same objects the live path would. Nothing is stubbed except
the socket.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.market_data.health import FeedState
from app.market_data.protocols import MarketDataError
from app.market_data.providers import upstox_proto as up
from app.market_data.session import Segment, SessionCalendar
from app.research.market_replay import (
    MarketDataReplay,
    read_length_prefixed,
    write_length_prefixed,
)
from app.storage.event_store import EventStore

FIXTURE = PROJECT_ROOT / "examples" / "fixtures" / "upstox-feed-session.binpb"
SYMBOL = "NSE:NIFTY"
SESSION_OPEN_IST = time(9, 15)
# 09:15 IST on Monday 2026-10-19 == 03:45 UTC.
SESSION_OPEN = datetime(2026, 10, 19, 3, 45, tzinfo=timezone.utc)


def millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


@pytest.fixture
def store(tmp_path):
    opened = EventStore(tmp_path / "market.db")
    yield opened
    opened.close()


def replay(store, **kwargs) -> MarketDataReplay:
    return MarketDataReplay(
        store,
        symbol=SYMBOL,
        timeframe="5m",
        session_open_ist=SESSION_OPEN_IST,
        **kwargs,
    )


# --- frame framing -------------------------------------------------------------


def test_frames_are_length_prefixed_because_protobuf_is_not_self_delimiting(tmp_path):
    """Concatenated messages merge instead of erroring, so framing is required.

    Two FeedResponse messages appended together parse as one message, with the
    second's fields overwriting the first's. A recording without length prefixes
    would therefore silently lose ticks rather than fail.
    """
    first = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    first.feeds[SYMBOL].ltpc.ltp = 100.0
    second = up.FeedResponse(
        type=up.Type.live_feed, currentTs=millis(SESSION_OPEN + timedelta(minutes=1))
    )
    second.feeds[SYMBOL].ltpc.ltp = 200.0

    merged = up.FeedResponse()
    merged.ParseFromString(first.SerializeToString() + second.SerializeToString())
    assert merged.feeds[SYMBOL].ltpc.ltp == 200.0  # the first price is gone

    path = tmp_path / "framed.binpb"
    written = write_length_prefixed(
        path, [first.SerializeToString(), second.SerializeToString()]
    )
    assert written == 2
    assert len(list(read_length_prefixed(path))) == 2


def test_a_truncated_recording_is_refused_not_partially_replayed(tmp_path):
    path = tmp_path / "truncated.binpb"
    frame = up.FeedResponse(type=up.Type.live_feed).SerializeToString()
    write_length_prefixed(path, [frame])
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="only"):
        list(read_length_prefixed(path))


def test_a_truncated_length_prefix_is_refused(tmp_path):
    path = tmp_path / "stub.binpb"
    path.write_bytes(b"\x00\x00")
    with pytest.raises(ValueError, match="truncated length prefix"):
        list(read_length_prefixed(path))


# --- the full route ------------------------------------------------------------


def test_the_recorded_session_runs_the_whole_route(store):
    engine = replay(store)
    summary = engine.run_file(FIXTURE)

    assert summary.frames == 15
    assert summary.decoded == 14  # one deliberately malformed frame
    assert summary.malformed == 1
    assert summary.reasons["MALFORMED_FRAME"] == 1
    assert summary.status_messages == 1
    assert summary.segment_status == {"NSE_EQ": "NORMAL_OPEN", "NSE_FO": "NORMAL_OPEN"}
    # Snapshot, eleven index prices, and the option frame's own last trade.
    assert summary.ticks == 13
    assert summary.quotes == 1  # the one option frame
    assert summary.candles_built >= 3
    assert summary.candles_stored == summary.candles_built
    assert summary.candles_duplicate == 0
    assert summary.final_feed_state == FeedState.HEALTHY.value


def test_a_malformed_frame_is_skipped_without_losing_the_rest(store):
    engine = replay(store)
    summary = engine.run_file(FIXTURE)
    # The malformed frame sits between the price path and the option quote; both
    # sides of it were processed.
    assert summary.malformed == 1
    assert summary.ticks > 0 and summary.quotes == 1


def test_strict_mode_raises_on_a_malformed_frame_instead_of_counting_it(store):
    engine = replay(store, strict=True)
    with pytest.raises(MarketDataError, match="not valid Protobuf"):
        engine.run_file(FIXTURE)


def test_candles_land_in_sqlite_with_their_provenance(store):
    engine = replay(store)
    engine.run_file(FIXTURE)
    rows = store.candles(symbol=SYMBOL, timeframe="5m")
    assert rows
    first = rows[0]
    assert first["symbol"] == SYMBOL
    assert first["timeframe"] == "5m"
    assert first["open_time"] == SESSION_OPEN.isoformat()
    assert first["close_time"] == (SESSION_OPEN + timedelta(minutes=5)).isoformat()
    assert first["open"] == 25_000.0
    assert first["high"] == 25_020.0
    assert first["low"] == 25_000.0
    assert first["close"] == 25_008.0
    assert first["provider"] == "upstox"
    assert first["is_mock"] is True  # fixture data must never read as live
    assert first["endpoint"] == "candle_builder"
    assert first["tick_count"] == 5


def test_the_candle_grid_matches_the_signal_contract_for_the_same_bar(store):
    from app.signals.timeframes import bar_close_from_open

    engine = replay(store)
    engine.run_file(FIXTURE)
    for candle in engine.candles:
        assert candle.close_time == bar_close_from_open(candle.open_time, "5")


def test_bar_boundaries_are_anchored_on_the_exchange_calendar(store):
    calendar = SessionCalendar.load(
        PROJECT_ROOT / "data" / "reference" / "nse_calendar.json"
    )
    engine = MarketDataReplay.for_calendar(
        store,
        calendar,
        symbol=SYMBOL,
        timeframe="5m",
        on=SESSION_OPEN,
        segment=Segment.FO,
    )
    engine.run_file(FIXTURE)
    # 09:15 IST is the anchor, so the first bar opens exactly at the session open.
    assert engine.candles[0].open_time == SESSION_OPEN


# --- determinism and no fabrication -------------------------------------------


def test_replaying_the_same_recording_twice_is_identical(tmp_path):
    results = []
    for run in range(2):
        opened = EventStore(tmp_path / f"run{run}.db")
        results.append(replay(opened).run_file(FIXTURE).as_dict())
        opened.close()
    assert results[0] == results[1]


def test_replaying_into_a_populated_store_stores_nothing_new(store):
    first = replay(store).run_file(FIXTURE)
    before = store.count_candles()
    second = replay(store).run_file(FIXTURE)
    assert first.candles_stored == before
    assert second.candles_stored == 0
    assert second.candles_duplicate == second.candles_built
    assert store.count_candles() == before


def test_a_stored_bar_is_never_rewritten(store):
    engine = replay(store)
    engine.run_file(FIXTURE)
    original = store.candles(symbol=SYMBOL)[0]

    # A second, different reading of the same interval must not replace a bar a
    # decision may already have been made on.
    from dataclasses import replace

    altered = replace(engine.candles[0], close=99_999.0, high=99_999.0)
    assert store.store_candle(altered) is False
    assert store.candles(symbol=SYMBOL)[0]["close"] == original["close"]


def test_empty_buckets_produce_no_candles(store):
    engine = replay(store)
    engine.run_file(FIXTURE)
    # The recording jumps from minute 11 to minute 25, so three 5m buckets got
    # no ticks at all. They are counted, never invented.
    assert engine.stats.buckets_skipped >= 2
    open_times = [candle.open_time for candle in engine.candles]
    assert len(set(open_times)) == len(open_times)
    assert all(candle.tick_count and candle.tick_count > 0 for candle in engine.candles)


def test_the_duplicate_tick_in_the_recording_is_counted_not_double_applied(store):
    engine = replay(store)
    engine.run_file(FIXTURE)
    assert engine.stats.duplicates == 1


def test_no_candle_is_stored_without_provenance(store):
    from dataclasses import replace

    engine = replay(store)
    engine.run_file(FIXTURE)
    shifted = SESSION_OPEN + timedelta(days=1)
    orphan = replace(
        engine.candles[0],
        provenance=None,
        open_time=shifted,
        close_time=shifted + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="no provenance"):
        store.store_candle(orphan)


# --- feed health through the replay -------------------------------------------


def test_the_watchdog_follows_the_recording_clock(store):
    engine = replay(store)
    summary = engine.run_file(FIXTURE)
    assert summary.first_message_time == SESSION_OPEN
    assert summary.last_message_time is not None
    # Health is judged at the last message, from the recording's own clock, so a
    # replay run months later still reports the session as healthy.
    assert engine.feed_state is FeedState.HEALTHY


def test_a_gap_longer_than_the_silence_limit_is_reported_silent(store):
    engine = replay(store, silence_limit=timedelta(seconds=30))
    frames = list(read_length_prefixed(FIXTURE))
    # The recording's own 14-minute gap exceeds a 30-second silence limit when
    # health is evaluated at the moment before the gap closes.
    engine.run(frames[:5])
    assert not engine._watchdog.is_usable(SESSION_OPEN + timedelta(minutes=10))


def test_a_recording_with_no_timestamps_leaves_the_feed_never_connected(store):
    response = up.FeedResponse(type=up.Type.live_feed)
    response.feeds[SYMBOL].ltpc.ltp = 25_000.0
    response.feeds[SYMBOL].ltpc.ltt = millis(SESSION_OPEN)
    engine = replay(store)
    summary = engine.run([response.SerializeToString()])
    assert summary.ticks == 1
    # No currentTs anywhere, so nothing ever confirmed a connection.
    assert summary.final_feed_state == FeedState.NEVER_CONNECTED.value


def test_a_frame_with_no_feeds_is_counted_as_a_rejected_update(store):
    engine = replay(store)
    summary = engine.run([up.FeedResponse(type=up.Type.live_feed).SerializeToString()])
    assert summary.rejected_updates == 1
    assert summary.reasons["NO_FEEDS"] == 1
    assert summary.candles_built == 0


def test_a_crossed_book_frame_is_counted_and_does_not_halt_the_replay(store):
    good = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    good.feeds[SYMBOL].ltpc.ltp = 25_000.0
    good.feeds[SYMBOL].ltpc.ltt = millis(SESSION_OPEN)

    crossed = up.FeedResponse(
        type=up.Type.live_feed, currentTs=millis(SESSION_OPEN + timedelta(minutes=1))
    )
    market = crossed.feeds[SYMBOL].fullFeed.marketFF
    market.ltpc.ltp = 25_010.0
    market.ltpc.ltt = millis(SESSION_OPEN + timedelta(minutes=1))
    market.marketLevel.bidAskQuote.add(bidP=25_020.0, askP=25_000.0, bidQ=1, askQ=1)

    engine = replay(store)
    summary = engine.run([good.SerializeToString(), crossed.SerializeToString()])
    assert summary.ticks == 1
    assert summary.rejected_updates == 1
    assert summary.reasons["CROSSED_BOOK"] == 1


def test_a_tick_before_the_session_open_is_counted_not_bucketed(store):
    before = up.FeedResponse(
        type=up.Type.live_feed, currentTs=millis(SESSION_OPEN - timedelta(minutes=10))
    )
    before.feeds[SYMBOL].ltpc.ltp = 25_000.0
    before.feeds[SYMBOL].ltpc.ltt = millis(SESSION_OPEN - timedelta(minutes=10))

    engine = replay(store)
    summary = engine.run([before.SerializeToString()])
    assert summary.ticks_before_session == 1
    assert summary.reasons["TICK_BEFORE_SESSION"] == 1
    assert summary.candles_built == 0
    assert store.count_candles() == 0


def test_option_quotes_are_normalized_alongside_index_ticks(store):
    engine = replay(store)
    engine.run_file(FIXTURE)
    assert len(engine.quotes) == 1
    quote = engine.quotes[0]
    assert quote.bid == 120.25 and quote.ask == 120.75
    assert quote.is_two_sided
    assert quote.provenance.is_mock is False  # the frame itself claims to be live
