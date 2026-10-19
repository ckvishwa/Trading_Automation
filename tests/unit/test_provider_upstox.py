"""Upstox adapter tests. Entirely offline: transports are injected, no sockets."""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import PROJECT_ROOT
from app.market_data.contracts import OptionType
from app.market_data.protocols import (
    CredentialsMissing,
    DataUnavailable,
    MarketDataError,
    RateLimited,
)
from app.market_data.providers import upstox

FIXTURES = PROJECT_ROOT / "examples" / "fixtures"
FETCHED_AT = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)
INDEX_KEY = "NSE_INDEX|Nifty 50"
SESSION_OPEN = datetime(2026, 10, 19, 3, 45, tzinfo=timezone.utc)


@pytest.fixture
def master() -> upstox.UpstoxInstrumentMaster:
    loaded = upstox.UpstoxInstrumentMaster()
    loaded.load_gz_path(FIXTURES / "upstox-instruments-sample.json.gz", FETCHED_AT)
    return loaded


# --- instrument master --------------------------------------------------------


def test_instrument_master_loads_the_fixture(master):
    assert len(master) == 4
    assert master.fetched_at == FETCHED_AT


def test_index_resolves_through_a_configured_alias(master):
    instrument = master.index_instrument("NSE:NIFTY")
    assert instrument.provider_key == INDEX_KEY
    assert instrument.segment == "NSE_INDEX"
    assert instrument.extra["instrument_type"] == "INDEX"
    assert instrument.provenance.is_mock is False
    assert instrument.provenance.provider == "upstox"


def test_an_unmapped_symbol_is_not_guessed(master):
    with pytest.raises(DataUnavailable, match="no Upstox alias configured"):
        master.resolve("NSE:MIDCPNIFTY")


def test_a_mapped_symbol_absent_from_the_file_raises(master):
    with pytest.raises(DataUnavailable, match="will not guess"):
        master.resolve("BSE:SENSEX")


def test_index_lookup_refuses_an_option(master):
    option = master.by_provider_key("NSE_FO|43309")
    master._aliases["TEST:OPT"] = (option.segment, option.extra["trading_symbol"])
    with pytest.raises(DataUnavailable, match="not INDEX"):
        master.index_instrument("TEST:OPT")


def test_option_fields_come_from_the_record_not_from_a_pattern(master):
    call = master.by_provider_key("NSE_FO|43309")
    assert call.option_type is OptionType.CE  # from instrument_type, not option_type
    assert call.strike == 24_000.0
    assert call.lot_size == 75
    assert call.tick_size == 0.05
    assert call.expiry == date(2026, 10, 30)  # 23:59:59Z is 05:29 IST the next day
    assert call.underlying_symbol == "NIFTY"
    assert call.extra["weekly"] is True


def test_option_expiries_are_listed_from_real_contracts(master):
    assert master.option_expiries("NSE:NIFTY") == (date(2026, 10, 30), date(2026, 11, 6))


def test_option_contracts_filter_by_expiry_and_type(master):
    both = master.option_contracts("NSE:NIFTY", date(2026, 10, 30))
    assert [instrument.option_type for instrument in both] == [OptionType.CE, OptionType.PE]
    calls = master.option_contracts("NSE:NIFTY", date(2026, 10, 30), OptionType.CE)
    assert len(calls) == 1 and calls[0].provider_key == "NSE_FO|43309"


def test_an_expiry_with_no_contracts_raises(master):
    with pytest.raises(DataUnavailable, match="no Upstox CE contracts"):
        master.option_contracts("NSE:NIFTY", date(2026, 12, 31), OptionType.CE)


def test_querying_before_loading_raises(master):
    empty = upstox.UpstoxInstrumentMaster()
    with pytest.raises(DataUnavailable, match="has not been loaded"):
        empty.resolve("NSE:NIFTY")


def test_refresh_without_a_transport_names_what_is_missing():
    with pytest.raises(CredentialsMissing, match="no HTTP transport"):
        upstox.UpstoxInstrumentMaster().refresh()


def test_refresh_uses_the_injected_transport(master):
    blob = (FIXTURES / "upstox-instruments-sample.json.gz").read_bytes()
    calls: list[str] = []

    def transport(url: str) -> bytes:
        calls.append(url)
        return blob

    loaded = upstox.UpstoxInstrumentMaster()
    stamp = loaded.refresh(transport=transport, now=FETCHED_AT)
    assert stamp == FETCHED_AT
    assert calls == [upstox.INSTRUMENT_FILES["complete"]]
    assert len(loaded) == 4


# --- malformed provider data --------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"instrument_key": None}, "should be"),
        ({"trading_symbol": None}, "should be"),
        ({"segment": 10}, "should be"),
    ],
)
def test_a_malformed_instrument_record_is_reported_not_defaulted(mutation, message):
    record = {
        "instrument_key": INDEX_KEY,
        "trading_symbol": "Nifty 50",
        "segment": "NSE_INDEX",
        "exchange": "NSE",
        "instrument_type": "INDEX",
    }
    record.update(mutation)
    with pytest.raises(MarketDataError, match=message):
        upstox.UpstoxInstrumentMaster().load_records([record], FETCHED_AT)


def test_a_missing_required_field_names_the_field():
    with pytest.raises(MarketDataError, match="missing 'instrument_type'"):
        upstox.UpstoxInstrumentMaster().load_records(
            [
                {
                    "instrument_key": INDEX_KEY,
                    "trading_symbol": "Nifty 50",
                    "segment": "NSE_INDEX",
                }
            ],
            FETCHED_AT,
        )


def test_a_non_array_instrument_file_is_rejected():
    blob = gzip.compress(json.dumps({"not": "an array"}).encode())
    with pytest.raises(MarketDataError, match="JSON array"):
        upstox.UpstoxInstrumentMaster().load_gz_bytes(blob, FETCHED_AT)


def test_a_file_that_is_not_gzipped_json_is_rejected():
    with pytest.raises(MarketDataError, match="not gzipped JSON"):
        upstox.UpstoxInstrumentMaster().load_gz_bytes(b"plain text", FETCHED_AT)


@pytest.mark.parametrize("value", [0, -1, "", "tomorrow", True, None])
def test_an_unparseable_expiry_raises(value):
    with pytest.raises(MarketDataError):
        upstox.expiry_to_date(value)


def test_expiry_accepts_both_seconds_and_milliseconds():
    millis = int(datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert upstox.expiry_to_date(millis) == date(2026, 10, 29)
    assert upstox.expiry_to_date(millis // 1000) == date(2026, 10, 29)


def test_expiry_is_taken_in_ist_not_utc():
    # 2026-10-29T19:00Z is 2026-10-30T00:30 IST. The exchange date is the 30th.
    stamp = int(datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert upstox.expiry_to_date(stamp) == date(2026, 10, 30)


def test_the_expiry_unit_is_flagged_unverified():
    # The documentation types `expiry` only as "date"; milliseconds is inferred.
    assert upstox.EXPIRY_UNIT_VERIFIED is False
    assert upstox.FEED_TIMEZONE_VERIFIED is False
    assert upstox.VERIFIED_AGAINST_LIVE_ACCOUNT is False


# --- rate limiting ------------------------------------------------------------


def test_the_standard_tier_matches_the_documented_numbers():
    tier = upstox.RATE_LIMIT_TIERS["standard"]
    assert (tier.per_second, tier.per_minute, tier.per_30_minutes) == (50, 500, 2000)
    orders = upstox.RATE_LIMIT_TIERS["order_placement"]
    assert orders.per_second == 10


def test_the_per_second_cap_is_enforced_locally():
    limiter = upstox.UpstoxRateLimiter("totp_login")  # 1/s, 10/min, 60/30min
    limiter.acquire(1000.0)
    with pytest.raises(RateLimited) as caught:
        limiter.acquire(1000.5)
    assert caught.value.retry_after_seconds == pytest.approx(0.5)
    # Once the window has rolled, the call is legal again.
    limiter.acquire(1001.0)


def test_the_per_minute_cap_is_enforced():
    limiter = upstox.UpstoxRateLimiter("totp_login")
    for index in range(10):
        limiter.acquire(1000.0 + index * 2)
    assert limiter.retry_after(1020.0) > 0
    assert not limiter.try_acquire(1020.0)


def test_retry_after_is_zero_when_a_call_is_allowed():
    limiter = upstox.UpstoxRateLimiter("standard")
    assert limiter.retry_after(1000.0) == 0.0


# --- subscription management --------------------------------------------------


def test_a_subscribe_request_matches_the_documented_shape():
    subscription = upstox.UpstoxSubscription()
    request = subscription.subscribe([INDEX_KEY], upstox.FeedMode.FULL, guid="abc123")
    assert request == {
        "guid": "abc123",
        "method": "sub",
        "data": {"mode": "full", "instrumentKeys": [INDEX_KEY]},
    }
    # Upstox requires a binary frame, not a text frame.
    assert json.loads(upstox.UpstoxSubscription.encode(request)) == request


def test_subscription_tracks_mode_per_instrument():
    subscription = upstox.UpstoxSubscription()
    subscription.subscribe([INDEX_KEY], upstox.FeedMode.LTPC)
    subscription.subscribe(["NSE_FO|43309"], upstox.FeedMode.FULL)
    assert subscription.mode_of(INDEX_KEY) is upstox.FeedMode.LTPC
    assert subscription.total == 2


def test_resubscribing_in_a_different_mode_is_refused():
    subscription = upstox.UpstoxSubscription()
    subscription.subscribe([INDEX_KEY], upstox.FeedMode.LTPC)
    with pytest.raises(MarketDataError, match="use change_mode"):
        subscription.subscribe([INDEX_KEY], upstox.FeedMode.FULL)


def test_change_mode_moves_an_instrument_and_builds_the_request():
    subscription = upstox.UpstoxSubscription()
    subscription.subscribe([INDEX_KEY], upstox.FeedMode.LTPC)
    request = subscription.change_mode([INDEX_KEY], upstox.FeedMode.FULL, guid="g2")
    assert request["method"] == "change_mode"
    assert request["data"] == {"mode": "full", "instrumentKeys": [INDEX_KEY]}
    assert subscription.mode_of(INDEX_KEY) is upstox.FeedMode.FULL


def test_change_mode_for_an_unsubscribed_key_is_refused():
    with pytest.raises(MarketDataError, match="unsubscribed keys"):
        upstox.UpstoxSubscription().change_mode([INDEX_KEY], upstox.FeedMode.FULL)


def test_unsubscribe_drops_the_instrument():
    subscription = upstox.UpstoxSubscription()
    subscription.subscribe([INDEX_KEY, "NSE_FO|43309"], upstox.FeedMode.LTPC)
    request = subscription.unsubscribe([INDEX_KEY])
    assert request["method"] == "unsub"
    assert "mode" not in request["data"]
    assert subscription.keys() == frozenset({"NSE_FO|43309"})


def test_the_individual_mode_cap_is_enforced():
    subscription = upstox.UpstoxSubscription()
    with pytest.raises(upstox.SubscriptionLimitExceeded, match="individual limit of 2000"):
        subscription.subscribe([f"K{i}" for i in range(2001)], upstox.FeedMode.FULL)


def test_the_combined_cap_applies_across_modes():
    subscription = upstox.UpstoxSubscription()
    subscription.subscribe([f"A{i}" for i in range(1400)], upstox.FeedMode.FULL)
    with pytest.raises(upstox.SubscriptionLimitExceeded, match="combined limit of 1500"):
        subscription.subscribe([f"B{i}" for i in range(200)], upstox.FeedMode.LTPC)


def test_full_d30_requires_the_plus_tier():
    subscription = upstox.UpstoxSubscription()
    with pytest.raises(upstox.SubscriptionLimitExceeded, match="Upstox Plus"):
        subscription.subscribe([INDEX_KEY], upstox.FeedMode.FULL_D30)
    plus = upstox.UpstoxSubscription(plus_tier=True)
    plus.subscribe([INDEX_KEY], upstox.FeedMode.FULL_D30)
    assert plus.total == 1


def test_connection_caps_match_the_documented_tiers():
    assert upstox.UpstoxSubscription().max_connections == 2
    assert upstox.UpstoxSubscription(plus_tier=True).max_connections == 5


def test_subscribing_nothing_is_a_programming_error():
    with pytest.raises(MarketDataError, match="at least one instrument key"):
        upstox.UpstoxSubscription().subscribe([])


# --- feed decoding and normalization ------------------------------------------


def frames(path: str) -> list[dict]:
    decoder = upstox.JsonFeedDecoder()
    return [
        decoder.decode(line.encode())
        for line in (FIXTURES / path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_the_protobuf_decoder_says_exactly_what_is_missing():
    with pytest.raises(CredentialsMissing) as caught:
        upstox.ProtobufFeedDecoder().decode(b"\x00")
    assert "MarketDataFeed.proto" in str(caught.value)
    assert "protoc" in str(caught.value)


def test_a_non_json_fixture_frame_is_rejected():
    with pytest.raises(MarketDataError, match="not JSON"):
        upstox.JsonFeedDecoder().decode(b"\x00\x01")


def test_market_info_messages_carry_no_updates():
    normalizer = upstox.UpstoxFeedNormalizer()
    assert normalizer.normalize_message(frames("upstox-feed-index-session.jsonl")[0]) == ()


def test_ltpc_frames_normalize_to_ticks():
    normalizer = upstox.UpstoxFeedNormalizer()
    updates = normalizer.normalize_message(frames("upstox-feed-index-session.jsonl")[1])
    assert len(updates) == 1
    update = updates[0]
    assert update.feed_mode == "ltpc"
    assert update.instrument_key == INDEX_KEY
    assert update.tick.last_price == 25_000.0
    assert update.tick.exchange_time == SESSION_OPEN
    assert update.tick.last_quantity == 50
    assert update.quote is None


def test_full_frames_normalize_to_two_sided_quotes():
    normalizer = upstox.UpstoxFeedNormalizer()
    update = normalizer.normalize_message(frames("upstox-feed-option-quotes.jsonl")[0])[0]
    assert update.feed_mode == "full"
    assert update.quote.bid == 120.25
    assert update.quote.ask == 120.75
    assert update.quote.bid_quantity == 750
    assert update.quote.is_two_sided
    assert update.quote.spread == pytest.approx(0.5)
    assert update.ohlc and update.ohlc[0]["interval"] == "I1"


def test_option_greeks_frames_normalize_their_first_depth():
    normalizer = upstox.UpstoxFeedNormalizer()
    message = {
        "type": "live_feed",
        "currentTs": str(int(SESSION_OPEN.timestamp() * 1000)),
        "feeds": {
            "NSE_FO|43309": {
                "firstLevelWithGreeks": {
                    "ltpc": {"ltp": 120.5, "ltt": str(int(SESSION_OPEN.timestamp() * 1000))},
                    "firstDepth": {"bidP": 120.2, "askP": 120.8, "bidQ": "75", "askQ": "150"},
                    "optionGreeks": {"delta": 0.52},
                }
            }
        },
    }
    update = normalizer.normalize_message(message)[0]
    assert update.feed_mode == "option_greeks"
    assert update.quote.bid == 120.2 and update.quote.ask == 120.8


def test_a_zero_priced_side_is_not_treated_as_a_bid():
    normalizer = upstox.UpstoxFeedNormalizer()
    message = {
        "type": "live_feed",
        "currentTs": str(int(SESSION_OPEN.timestamp() * 1000)),
        "feeds": {
            "NSE_FO|43309": {
                "fullFeed": {
                    "marketFF": {
                        "ltpc": {"ltp": 120.5, "ltt": str(int(SESSION_OPEN.timestamp() * 1000))},
                        "marketLevel": {"bidAskQuote": [{"bidP": 0, "askP": 120.8}]},
                    }
                }
            }
        },
    }
    quote = normalizer.normalize_message(message)[0].quote
    assert quote.bid is None
    assert not quote.is_two_sided
    assert quote.spread is None


def test_a_crossed_book_from_the_feed_is_an_error():
    normalizer = upstox.UpstoxFeedNormalizer()
    message = {
        "type": "live_feed",
        "currentTs": str(int(SESSION_OPEN.timestamp() * 1000)),
        "feeds": {
            "NSE_FO|43309": {
                "fullFeed": {
                    "marketFF": {
                        "ltpc": {"ltp": 120.5, "ltt": str(int(SESSION_OPEN.timestamp() * 1000))},
                        "marketLevel": {"bidAskQuote": [{"bidP": 121.0, "askP": 120.0}]},
                    }
                }
            }
        },
    }
    with pytest.raises(MarketDataError, match="crossed book"):
        normalizer.normalize_message(message)


@pytest.mark.parametrize(
    ("ltpc", "message"),
    [
        ({"ltt": "1"}, "no 'ltp'"),
        ({"ltp": "abc"}, "non-numeric ltp"),
        ({"ltp": 0}, "non-positive ltp"),
        ({"ltp": -5}, "non-positive ltp"),
    ],
)
def test_malformed_ltpc_raises_rather_than_producing_a_price(ltpc, message):
    normalizer = upstox.UpstoxFeedNormalizer()
    frame = {"type": "live_feed", "feeds": {INDEX_KEY: {"ltpc": ltpc}}}
    with pytest.raises(MarketDataError, match=message):
        normalizer.normalize_message(frame)


def test_an_unrecognised_feed_payload_shape_raises():
    normalizer = upstox.UpstoxFeedNormalizer()
    with pytest.raises(MarketDataError, match="unrecognised feed payload"):
        normalizer.normalize_message(
            {"type": "live_feed", "feeds": {INDEX_KEY: {"somethingElse": {}}}}
        )


def test_a_message_without_feeds_raises():
    with pytest.raises(MarketDataError, match="no 'feeds'"):
        upstox.UpstoxFeedNormalizer().normalize_message({"type": "live_feed"})


def test_a_quote_timestamp_far_from_the_message_clock_is_rejected():
    """Quote timestamp mismatch.

    A per-instrument timestamp far from the message's own `currentTs` means
    something stale is being replayed into a live stream. Averaging it away
    would hide exactly the fault that matters.
    """
    normalizer = upstox.UpstoxFeedNormalizer(max_quote_timestamp_skew=timedelta(seconds=30))
    stale = SESSION_OPEN - timedelta(minutes=10)
    message = {
        "type": "live_feed",
        "currentTs": str(int(SESSION_OPEN.timestamp() * 1000)),
        "feeds": {
            INDEX_KEY: {
                "ltpc": {"ltp": 25_000.0, "ltt": str(int(stale.timestamp() * 1000))}
            }
        },
    }
    with pytest.raises(MarketDataError, match="beyond the 30s tolerance"):
        normalizer.normalize_message(message)


def test_a_small_timestamp_drift_is_tolerated():
    normalizer = upstox.UpstoxFeedNormalizer(max_quote_timestamp_skew=timedelta(seconds=30))
    drifted = SESSION_OPEN - timedelta(seconds=5)
    message = {
        "type": "live_feed",
        "currentTs": str(int(SESSION_OPEN.timestamp() * 1000)),
        "feeds": {
            INDEX_KEY: {
                "ltpc": {"ltp": 25_000.0, "ltt": str(int(drifted.timestamp() * 1000))}
            }
        },
    }
    assert normalizer.normalize_message(message)[0].tick.exchange_time == drifted


@pytest.mark.parametrize("value", ["", "abc", 0, -1, None, True])
def test_unparseable_feed_timestamps_raise(value):
    with pytest.raises(MarketDataError):
        upstox.epoch_millis_to_utc(value)


# --- replayable end-to-end path ------------------------------------------------


def replay_index_session():
    """Fixture frames -> normalizer -> candle builder, the live path's order."""
    from datetime import time as clock_time

    from app.market_data.candle_builder import CandleBuilder

    normalizer = upstox.UpstoxFeedNormalizer(key_to_symbol=lambda key: "NSE:NIFTY")
    builder = CandleBuilder(
        "NSE:NIFTY", "5m", clock_time(9, 15), provider_name="upstox", is_mock=True
    )
    candles = []
    last_tick = None
    for frame in frames("upstox-feed-index-session.jsonl"):
        for update in normalizer.normalize_message(frame):
            if update.tick is None:
                continue
            last_tick = update.tick
            completed = builder.add(update.tick)
            if completed is not None:
                candles.append(completed)
    if last_tick is not None:
        candles.extend(builder.close_through(last_tick.exchange_time + timedelta(minutes=5)))
    return candles, builder.stats


def test_the_fixture_session_builds_the_expected_candles():
    candles, stats = replay_index_session()
    # The fixture's last tick lands at minute 10, which opens a third bar; the
    # explicit close_through then completes it.
    assert [candle.open_time for candle in candles] == [
        SESSION_OPEN,
        SESSION_OPEN + timedelta(minutes=5),
        SESSION_OPEN + timedelta(minutes=10),
    ]
    assert candles[-1].tick_count == 1
    first = candles[0]
    assert (first.open, first.high, first.low, first.close) == (
        25_000.0,
        25_020.0,
        25_000.0,
        25_008.0,
    )
    assert first.provenance.provider == "upstox"


def test_the_fixture_session_counts_its_duplicate_and_keeps_order_tolerance():
    _, stats = replay_index_session()
    # One deliberate duplicate tick, one deliberate out-of-order tick inside the
    # open bar: the duplicate is counted, the out-of-order one is accepted.
    assert stats.duplicates == 1
    assert stats.late == 0
    assert stats.accepted == 10


def test_replaying_the_fixture_twice_is_identical():
    first, _ = replay_index_session()
    second, _ = replay_index_session()
    assert [
        (c.open_time, c.open, c.high, c.low, c.close, c.tick_count) for c in first
    ] == [(c.open_time, c.open, c.high, c.low, c.close, c.tick_count) for c in second]


# --- historical candle paths ---------------------------------------------------


def test_the_historical_path_puts_to_date_before_from_date():
    path = upstox.historical_candle_path(
        INDEX_KEY, "minutes", 5, date(2026, 10, 19), date(2026, 9, 19)
    )
    assert path.index("2026-10-19") < path.index("2026-09-19")
    assert "%7C" in path and "|" not in path


def test_reversed_dates_are_refused():
    with pytest.raises(ValueError, match="after to_date"):
        upstox.historical_candle_path(
            INDEX_KEY, "minutes", 5, date(2026, 9, 19), date(2026, 10, 19)
        )


@pytest.mark.parametrize(
    ("unit", "interval"),
    [("minutes", 0), ("minutes", 301), ("hours", 6), ("days", 2), ("fortnights", 1)],
)
def test_undocumented_units_and_intervals_are_refused(unit, interval):
    with pytest.raises(ValueError):
        upstox.historical_candle_path(
            INDEX_KEY, unit, interval, date(2026, 10, 19), date(2026, 10, 1)
        )


def test_request_caps_follow_the_documented_split_at_15_minutes():
    assert upstox.max_request_days("minutes", 15) == 31
    assert upstox.max_request_days("minutes", 16) == 92
    assert upstox.max_request_days("hours", 1) == 92
    assert upstox.max_request_days("days", 1) == 3653


def test_history_start_dates_match_the_documentation():
    assert upstox.earliest_available("minutes") == date(2022, 1, 1)
    assert upstox.earliest_available("days") == date(2000, 1, 1)
