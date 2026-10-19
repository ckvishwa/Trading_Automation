"""Decoding of real Protobuf frames built from Upstox's official V3 schema.

Every frame here is serialized by the generated classes, so these are genuine
wire bytes rather than a hand-rolled approximation of them. The schema is
vendored with its SHA256; `test_the_vendored_schema_matches_its_lock` fails if
either drifts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.market_data.contracts import Quote, Tick
from app.market_data.protocols import MarketDataError
from app.market_data.providers import upstox
from app.market_data.providers import upstox_proto as up

PACKAGE = Path(up.__file__).resolve().parent
PROTO_PATH = PACKAGE / "MarketDataFeedV3.proto"
LOCK_PATH = PACKAGE / "SCHEMA.lock.json"

INDEX_KEY = "NSE_INDEX|Nifty 50"
OPTION_KEY = "NSE_FO|43309"
# 09:15 IST on 2026-10-19 == 03:45 UTC.
SESSION_OPEN = datetime(2026, 10, 19, 3, 45, tzinfo=timezone.utc)


def millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


@pytest.fixture
def decoder() -> upstox.ProtobufFeedDecoder:
    return upstox.ProtobufFeedDecoder()


@pytest.fixture
def normalizer() -> upstox.UpstoxFeedNormalizer:
    return upstox.UpstoxFeedNormalizer(key_to_symbol=lambda key: "NSE:NIFTY")


# --- schema provenance ---------------------------------------------------------


def test_the_vendored_schema_matches_its_lock():
    lock = json.loads(LOCK_PATH.read_text("utf-8"))
    digest = hashlib.sha256(PROTO_PATH.read_bytes()).hexdigest()
    assert digest == lock["sha256"]
    assert lock["bytes"] == PROTO_PATH.stat().st_size
    assert lock["schema_url"] == upstox.FEED_PROTO_URL
    assert lock["proto_package"] == "com.upstox.marketdatafeederv3udapi.rpc.proto"
    assert lock["generator_versions"] == {"protobuf": "5.29.3", "grpcio-tools": "1.68.1"}


def test_the_schema_declares_the_messages_the_decoder_relies_on():
    # If Upstox renames or renumbers these, decoding is wrong rather than broken,
    # so the names are asserted rather than assumed.
    for name in (
        "FeedResponse",
        "Feed",
        "FullFeed",
        "MarketFullFeed",
        "IndexFullFeed",
        "FirstLevelWithGreeks",
        "LTPC",
        "Quote",
        "MarketInfo",
    ):
        assert hasattr(up.pb2, name), name


def test_the_schema_enums_match_what_the_adapter_assumes():
    assert [up.Type.Name(value) for value in (0, 1, 2)] == [
        "initial_feed",
        "live_feed",
        "market_info",
    ]
    # The schema spells the five-level book `full_d5`; the subscription API calls
    # that mode `full`. Both spellings are real and must not be conflated.
    assert [up.RequestMode.Name(value) for value in (0, 1, 2, 3)] == [
        "ltpc",
        "full_d5",
        "option_greeks",
        "full_d30",
    ]
    assert up.MarketStatus.Name(2) == "NORMAL_OPEN"


# --- market status -------------------------------------------------------------


def test_a_market_status_frame_decodes_with_its_segment_statuses(decoder):
    response = up.FeedResponse(type=up.Type.market_info, currentTs=millis(SESSION_OPEN))
    response.marketInfo.segmentStatus["NSE_EQ"] = up.MarketStatus.NORMAL_OPEN
    response.marketInfo.segmentStatus["NSE_FO"] = up.MarketStatus.PRE_OPEN_START

    decoded = decoder.decode(response.SerializeToString())
    assert decoded["type"] == "market_info"
    assert decoded["currentTs"] == str(millis(SESSION_OPEN))
    assert decoded["marketInfo"]["segmentStatus"] == {
        "NSE_EQ": "NORMAL_OPEN",
        "NSE_FO": "PRE_OPEN_START",
    }


def test_a_market_status_frame_yields_no_price_updates(decoder, normalizer):
    response = up.FeedResponse(type=up.Type.market_info)
    response.marketInfo.segmentStatus["NSE_EQ"] = up.MarketStatus.NORMAL_CLOSE
    assert normalizer.normalize_message(decoder.decode(response.SerializeToString())) == ()


def test_a_closing_status_is_not_silently_read_as_open(decoder):
    response = up.FeedResponse(type=up.Type.market_info)
    response.marketInfo.segmentStatus["NSE_FO"] = up.MarketStatus.CLOSING_END
    decoded = decoder.decode(response.SerializeToString())
    assert decoded["marketInfo"]["segmentStatus"]["NSE_FO"] == "CLOSING_END"


# --- initial snapshot ----------------------------------------------------------


def test_an_initial_snapshot_is_labelled_even_though_its_enum_is_zero(decoder):
    """proto3 omits a zero enum, and `initial_feed` is zero.

    Without the decoder setting it explicitly, a snapshot would arrive with no
    `type` at all and be indistinguishable from a malformed frame.
    """
    response = up.FeedResponse(type=up.Type.initial_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[INDEX_KEY]
    feed.ltpc.ltp = 25_000.0
    feed.ltpc.ltt = millis(SESSION_OPEN)

    decoded = decoder.decode(response.SerializeToString())
    assert decoded["type"] == "initial_feed"
    assert decoded["feeds"][INDEX_KEY]["ltpc"]["ltp"] == 25_000.0


def test_an_initial_snapshot_normalizes_like_a_live_update(decoder, normalizer):
    response = up.FeedResponse(type=up.Type.initial_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[INDEX_KEY]
    feed.ltpc.ltp = 25_000.0
    feed.ltpc.ltt = millis(SESSION_OPEN)

    updates = normalizer.normalize_message(decoder.decode(response.SerializeToString()))
    assert len(updates) == 1
    assert updates[0].message_type == "initial_feed"
    assert isinstance(updates[0].tick, Tick)
    assert updates[0].tick.last_price == 25_000.0


# --- LTPC updates --------------------------------------------------------------


def test_an_ltpc_update_decodes_with_64_bit_fields_as_strings(decoder):
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[INDEX_KEY]
    feed.ltpc.ltp = 25_100.25
    feed.ltpc.ltt = millis(SESSION_OPEN)
    feed.ltpc.ltq = 50
    feed.ltpc.cp = 24_980.0

    ltpc = decoder.decode(response.SerializeToString())["feeds"][INDEX_KEY]["ltpc"]
    # The documented examples show millisecond timestamps as strings; protobuf's
    # own JSON mapping does that for every 64-bit field, which is why a decoded
    # frame and a recorded JSON fixture take one code path.
    assert ltpc["ltt"] == str(millis(SESSION_OPEN))
    assert ltpc["ltq"] == "50"
    assert ltpc["ltp"] == 25_100.25
    assert ltpc["cp"] == 24_980.0


def test_an_ltpc_update_normalizes_to_a_tick_with_the_exchange_time(decoder, normalizer):
    moment = SESSION_OPEN + timedelta(seconds=37)
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(moment))
    feed = response.feeds[INDEX_KEY]
    feed.ltpc.ltp = 25_100.25
    feed.ltpc.ltt = millis(moment)
    feed.ltpc.ltq = 50

    update = normalizer.normalize_message(decoder.decode(response.SerializeToString()))[0]
    assert update.tick.exchange_time == moment
    assert update.tick.last_quantity == 50
    assert update.quote is None
    assert update.request_mode == "ltpc"


def test_millisecond_precision_survives_the_round_trip(decoder, normalizer):
    moment = SESSION_OPEN + timedelta(milliseconds=1234)
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(moment))
    feed = response.feeds[INDEX_KEY]
    feed.ltpc.ltp = 25_000.5
    feed.ltpc.ltt = millis(moment)

    update = normalizer.normalize_message(decoder.decode(response.SerializeToString()))[0]
    assert update.tick.exchange_time == moment
    assert update.tick.exchange_time.microsecond == 234_000


def test_an_ltpc_with_no_last_price_is_rejected_not_defaulted(decoder, normalizer):
    # proto3 omits a zero double, so an LTPC with no price decodes to an empty
    # object. Defaulting that to 0.0 would reach the paper broker as a fill.
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    response.feeds[INDEX_KEY].ltpc.ltt = millis(SESSION_OPEN)

    decoded = decoder.decode(response.SerializeToString())
    with pytest.raises(MarketDataError, match="no 'ltp'"):
        normalizer.normalize_message(decoded)


# --- full market option quotes and depth --------------------------------------


def build_option_full_feed(
    *, bid: float = 120.25, ask: float = 120.75, depth_levels: int = 5
) -> up.FeedResponse:
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[OPTION_KEY]
    feed.requestMode = up.RequestMode.full_d5
    market = feed.fullFeed.marketFF
    market.ltpc.ltp = (bid + ask) / 2
    market.ltpc.ltt = millis(SESSION_OPEN)
    market.ltpc.ltq = 75
    for level in range(depth_levels):
        market.marketLevel.bidAskQuote.add(
            bidQ=750 + level * 10,
            bidP=round(bid - level * 0.05, 2),
            askQ=900 + level * 10,
            askP=round(ask + level * 0.05, 2),
        )
    market.optionGreeks.delta = 0.52
    market.optionGreeks.theta = -8.1
    market.marketOHLC.ohlc.add(
        interval="I1",
        open=bid,
        high=ask,
        low=bid,
        close=ask,
        vol=1200,
        ts=millis(SESSION_OPEN),
    )
    market.atp = (bid + ask) / 2
    market.vtt = 45_000
    market.oi = 1_500_000
    market.iv = 0.1187
    return response


def test_a_full_market_feed_decodes_option_fields(decoder):
    decoded = decoder.decode(build_option_full_feed().SerializeToString())
    market = decoded["feeds"][OPTION_KEY]["fullFeed"]["marketFF"]
    assert market["oi"] == 1_500_000
    assert market["iv"] == pytest.approx(0.1187)
    assert market["vtt"] == "45000"
    assert market["optionGreeks"]["delta"] == 0.52
    assert market["marketOHLC"]["ohlc"][0]["interval"] == "I1"
    assert decoded["feeds"][OPTION_KEY]["requestMode"] == "full_d5"


def test_five_levels_of_depth_decode_in_order(decoder):
    decoded = decoder.decode(build_option_full_feed().SerializeToString())
    quotes = decoded["feeds"][OPTION_KEY]["fullFeed"]["marketFF"]["marketLevel"][
        "bidAskQuote"
    ]
    assert len(quotes) == 5
    assert [quote["bidP"] for quote in quotes] == [120.25, 120.2, 120.15, 120.1, 120.05]
    assert [quote["askP"] for quote in quotes] == [120.75, 120.8, 120.85, 120.9, 120.95]


def test_thirty_levels_of_depth_decode(decoder):
    response = build_option_full_feed(depth_levels=30)
    response.feeds[OPTION_KEY].requestMode = up.RequestMode.full_d30
    decoded = decoder.decode(response.SerializeToString())
    quotes = decoded["feeds"][OPTION_KEY]["fullFeed"]["marketFF"]["marketLevel"][
        "bidAskQuote"
    ]
    assert len(quotes) == 30
    assert decoded["feeds"][OPTION_KEY]["requestMode"] == "full_d30"


def test_a_full_market_feed_normalizes_to_a_two_sided_quote(decoder, normalizer):
    update = normalizer.normalize_message(
        decoder.decode(build_option_full_feed().SerializeToString())
    )[0]
    assert isinstance(update.quote, Quote)
    # The best level is used; deeper levels are carried but not averaged in.
    assert update.quote.bid == 120.25
    assert update.quote.ask == 120.75
    assert update.quote.bid_quantity == 750
    assert update.quote.ask_quantity == 900
    assert update.quote.spread == pytest.approx(0.5)
    assert update.feed_mode == "full"
    assert update.request_mode == "full_d5"
    assert update.ohlc[0]["interval"] == "I1"


def test_a_crossed_book_on_the_wire_is_an_error(decoder, normalizer):
    response = build_option_full_feed(bid=121.0, ask=120.0, depth_levels=1)
    with pytest.raises(MarketDataError, match="crossed book"):
        normalizer.normalize_message(decoder.decode(response.SerializeToString()))


def test_a_one_sided_book_produces_no_tradable_spread(decoder, normalizer):
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[OPTION_KEY]
    market = feed.fullFeed.marketFF
    market.ltpc.ltp = 120.5
    market.ltpc.ltt = millis(SESSION_OPEN)
    # Only an ask: proto3 omits the zero bid, so the decoded quote has no bid.
    market.marketLevel.bidAskQuote.add(askQ=900, askP=120.75)

    quote = normalizer.normalize_message(decoder.decode(response.SerializeToString()))[0].quote
    assert quote.ask == 120.75
    assert quote.bid is None
    assert not quote.is_two_sided
    assert quote.spread is None


def test_an_option_greeks_feed_normalizes_its_first_depth(decoder, normalizer):
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[OPTION_KEY]
    feed.requestMode = up.RequestMode.option_greeks
    greeks = feed.firstLevelWithGreeks
    greeks.ltpc.ltp = 120.5
    greeks.ltpc.ltt = millis(SESSION_OPEN)
    greeks.firstDepth.bidP = 120.2
    greeks.firstDepth.bidQ = 75
    greeks.firstDepth.askP = 120.8
    greeks.firstDepth.askQ = 150
    greeks.optionGreeks.delta = 0.52
    greeks.oi = 1_500_000
    greeks.iv = 0.1187

    update = normalizer.normalize_message(decoder.decode(response.SerializeToString()))[0]
    assert update.feed_mode == "option_greeks"
    assert update.request_mode == "option_greeks"
    assert update.quote.bid == 120.2 and update.quote.ask == 120.8


def test_an_index_full_feed_has_no_depth_by_schema(decoder, normalizer):
    """`IndexFullFeed` holds only ltpc and marketOHLC in the official schema.

    So an index subscribed in full mode yields a tick and no quote. That is the
    schema's shape, not a missing book, and the normalizer must not invent one.
    """
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[INDEX_KEY]
    index = feed.fullFeed.indexFF
    index.ltpc.ltp = 25_100.25
    index.ltpc.ltt = millis(SESSION_OPEN)
    index.marketOHLC.ohlc.add(
        interval="1d", open=25_000.0, high=25_150.0, low=24_950.0, close=25_100.25
    )

    update = normalizer.normalize_message(decoder.decode(response.SerializeToString()))[0]
    assert update.tick.last_price == 25_100.25
    assert update.quote is None
    assert update.ohlc[0]["interval"] == "1d"


# --- malformed, empty and unknown ---------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        b"\xff\xff\xff\xff\xff\xff",
        b"\x08",  # tag with no value
        b"\x12\x7f",  # length-delimited field claiming 127 bytes that are absent
        b"not protobuf at all",
    ],
)
def test_malformed_binary_frames_raise(decoder, frame):
    with pytest.raises(MarketDataError, match="not valid Protobuf"):
        decoder.decode(frame)


def test_an_empty_frame_decodes_to_an_untyped_snapshot_with_no_feeds(decoder):
    # Valid protobuf: zero bytes is a message with every field unset.
    decoded = decoder.decode(b"")
    assert decoded == {"type": "initial_feed"}


def test_an_empty_frame_is_reported_rather_than_treated_as_no_change(
    decoder, normalizer
):
    with pytest.raises(MarketDataError, match="no 'feeds'"):
        normalizer.normalize_message(decoder.decode(b""))


def test_a_feed_entry_with_nothing_in_it_is_reported(decoder, normalizer):
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    response.feeds[INDEX_KEY].SetInParent()  # present, but holds no oneof member
    with pytest.raises(MarketDataError, match="carries no ltpc, fullFeed"):
        normalizer.normalize_message(decoder.decode(response.SerializeToString()))


def test_a_truncated_ltpc_submessage_is_reported(decoder):
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[INDEX_KEY]
    feed.ltpc.ltp = 25_000.0
    feed.ltpc.ltt = millis(SESSION_OPEN)
    blob = response.SerializeToString()
    with pytest.raises(MarketDataError, match="not valid Protobuf"):
        decoder.decode(blob[:-3])


def test_unknown_optional_fields_are_ignored_not_fatal(decoder):
    """A future additive schema change must not break this decoder.

    Field 99 does not exist in the vendored schema. proto3 keeps such fields in
    an unknown-field set and the decode succeeds, which is what makes an
    additive Upstox change safe to receive before regenerating.
    """
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[INDEX_KEY]
    feed.ltpc.ltp = 25_000.0
    feed.ltpc.ltt = millis(SESSION_OPEN)
    def varint(value: int) -> bytes:
        out = bytearray()
        while value > 0x7F:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value)
        return bytes(out)

    # Field 99, wire type 0 (varint), value 7. The tag itself is a varint, so
    # field numbers above 15 do not fit in a single byte.
    extra = varint((99 << 3) | 0) + varint(7)
    decoded = decoder.decode(response.SerializeToString() + extra)
    assert decoded["type"] == "live_feed"
    assert decoded["feeds"][INDEX_KEY]["ltpc"]["ltp"] == 25_000.0


def test_an_unknown_enum_value_does_not_crash_the_decode(decoder):
    # Upstox adding a MarketStatus member is additive; an unmapped number must
    # decode rather than raise, so it reaches a log instead of killing the feed.
    response = up.FeedResponse(type=up.Type.market_info)
    response.marketInfo.segmentStatus["NSE_EQ"] = 99
    decoded = decoder.decode(response.SerializeToString())
    assert decoded["marketInfo"]["segmentStatus"]["NSE_EQ"] in (99, "99")


def test_a_quote_timestamp_far_from_the_frame_clock_is_rejected(decoder):
    normalizer = upstox.UpstoxFeedNormalizer(
        key_to_symbol=lambda key: "NSE:NIFTY",
        max_quote_timestamp_skew=timedelta(seconds=30),
    )
    stale = SESSION_OPEN - timedelta(minutes=10)
    response = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    feed = response.feeds[INDEX_KEY]
    feed.ltpc.ltp = 25_000.0
    feed.ltpc.ltt = millis(stale)
    with pytest.raises(MarketDataError, match="beyond the 30s tolerance"):
        normalizer.normalize_message(decoder.decode(response.SerializeToString()))


# --- disconnection recovery ----------------------------------------------------


def test_decoding_resumes_after_a_disconnection(decoder, normalizer):
    """A dropped socket must not leave the decoder in a bad state.

    The decoder is stateless across frames by construction; this pins that, so a
    reconnect followed by a fresh snapshot decodes normally.
    """
    from app.market_data.health import FeedState, FeedWatchdog
    from app.market_data.protocols import FeedDisconnected

    watchdog = FeedWatchdog(silence_limit=timedelta(seconds=30))
    watchdog.on_connect(SESSION_OPEN)

    first = up.FeedResponse(type=up.Type.live_feed, currentTs=millis(SESSION_OPEN))
    first.feeds[INDEX_KEY].ltpc.ltp = 25_000.0
    first.feeds[INDEX_KEY].ltpc.ltt = millis(SESSION_OPEN)
    update = normalizer.normalize_message(decoder.decode(first.SerializeToString()))[0]
    watchdog.on_message(update.tick.exchange_time)
    assert watchdog.is_usable(SESSION_OPEN + timedelta(seconds=5))

    # The stream drops mid-session.
    watchdog.on_disconnect()
    with pytest.raises(FeedDisconnected):
        raise FeedDisconnected("socket closed by peer")
    assert watchdog.state(SESSION_OPEN + timedelta(seconds=6)) is FeedState.DISCONNECTED
    assert not watchdog.is_usable(SESSION_OPEN + timedelta(seconds=6))

    # A malformed frame arriving on the way down must not poison the decoder.
    with pytest.raises(MarketDataError):
        decoder.decode(b"\xff\xff")

    reconnected = SESSION_OPEN + timedelta(seconds=20)
    snapshot = up.FeedResponse(type=up.Type.initial_feed, currentTs=millis(reconnected))
    snapshot.feeds[INDEX_KEY].ltpc.ltp = 25_050.0
    snapshot.feeds[INDEX_KEY].ltpc.ltt = millis(reconnected)
    watchdog.on_connect(reconnected)
    resumed = normalizer.normalize_message(decoder.decode(snapshot.SerializeToString()))[0]
    assert resumed.message_type == "initial_feed"
    assert resumed.tick.last_price == 25_050.0
    assert watchdog.is_usable(reconnected + timedelta(seconds=1))
