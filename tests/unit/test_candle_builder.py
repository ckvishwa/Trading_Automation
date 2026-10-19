from datetime import datetime, time, timedelta, timezone

import pytest

from app.market_data.candle_builder import CandleBuilder, TickRejected
from app.market_data.contracts import Tick

SESSION_OPEN = time(9, 15)
SYMBOL = "NSE:NIFTY"
# 03:45Z == 09:15 IST, the first bar of the session.
OPEN_UTC = datetime(2026, 10, 20, 3, 45, tzinfo=timezone.utc)


def tick(minutes: float, price: float, **kwargs) -> Tick:
    return Tick(
        symbol=SYMBOL,
        exchange_time=OPEN_UTC + timedelta(minutes=minutes),
        last_price=price,
        **kwargs,
    )


def builder(timeframe: str = "5m") -> CandleBuilder:
    return CandleBuilder(SYMBOL, timeframe, SESSION_OPEN, provider_name="test", is_mock=True)


def test_first_bucket_starts_at_the_session_open():
    b = builder()
    assert b.bucket_open_for(OPEN_UTC) == OPEN_UTC
    assert b.bucket_open_for(OPEN_UTC + timedelta(minutes=4, seconds=59)) == OPEN_UTC
    assert b.bucket_open_for(OPEN_UTC + timedelta(minutes=5)) == OPEN_UTC + timedelta(minutes=5)


def test_buckets_anchor_on_session_open_not_utc_midnight():
    # A 10m NSE bar opens 09:15, 09:25 IST. 09:20 IST belongs to the 09:15 bar,
    # even though 09:20 IST is 03:50 UTC, which IS on a UTC 10-minute grid.
    b = builder("10m")
    assert b.bucket_open_for(OPEN_UTC + timedelta(minutes=5)) == OPEN_UTC
    assert b.bucket_open_for(OPEN_UTC + timedelta(minutes=10)) == OPEN_UTC + timedelta(minutes=10)


def test_no_candle_is_emitted_while_the_bar_is_still_open():
    b = builder()
    assert b.add(tick(0, 100.0)) is None
    assert b.add(tick(2, 105.0)) is None
    assert b.add(tick(4, 102.0)) is None
    assert b.stats.candles_emitted == 0
    assert b.open_bar_close_time == OPEN_UTC + timedelta(minutes=5)


def test_a_tick_in_the_next_bucket_completes_the_previous_bar():
    b = builder()
    b.add(tick(0, 100.0))
    b.add(tick(1, 110.0))
    b.add(tick(2, 95.0))
    b.add(tick(4, 104.0))
    candle = b.add(tick(5, 106.0))
    assert candle is not None
    assert (candle.open, candle.high, candle.low, candle.close) == (100.0, 110.0, 95.0, 104.0)
    assert candle.open_time == OPEN_UTC
    assert candle.close_time == OPEN_UTC + timedelta(minutes=5)
    assert candle.tick_count == 4
    assert candle.timeframe == "5m"


def test_close_through_emits_only_once_the_interval_has_ended():
    b = builder()
    b.add(tick(0, 100.0))
    assert b.close_through(OPEN_UTC + timedelta(minutes=4)) == []
    emitted = b.close_through(OPEN_UTC + timedelta(minutes=5))
    assert len(emitted) == 1
    assert emitted[0].close_time == OPEN_UTC + timedelta(minutes=5)
    # Nothing is left open, so a further assertion of time emits nothing.
    assert b.close_through(OPEN_UTC + timedelta(minutes=20)) == []


def test_empty_buckets_are_counted_and_never_fabricated():
    b = builder()
    b.add(tick(0, 100.0))
    # Next tick is 20 minutes later: three 5m buckets received no ticks at all.
    candle = b.add(tick(20, 120.0))
    assert candle is not None and candle.open_time == OPEN_UTC
    assert b.stats.buckets_skipped == 3
    assert b.stats.candles_emitted == 1  # not 4


def test_duplicate_ticks_are_counted_and_do_not_affect_ohlc():
    b = builder()
    b.add(tick(0, 100.0))
    repeated = tick(1, 130.0, cumulative_volume=10)
    b.add(repeated)
    b.add(repeated)
    b.add(repeated)
    candle = b.close_through(OPEN_UTC + timedelta(minutes=5))[0]
    assert b.stats.duplicates == 2
    assert candle.tick_count == 2
    assert candle.high == 130.0


def test_same_timestamp_with_a_different_price_is_not_a_duplicate():
    b = builder()
    b.add(tick(0, 100.0))
    b.add(tick(1, 101.0))
    b.add(tick(1, 109.0))
    candle = b.close_through(OPEN_UTC + timedelta(minutes=5))[0]
    assert b.stats.duplicates == 0
    assert candle.high == 109.0


def test_a_tick_for_a_closed_bar_is_dropped_as_late():
    b = builder()
    b.add(tick(0, 100.0))
    b.add(tick(5, 200.0))  # closes the first bar
    assert b.add(tick(3, 999.0)) is None
    assert b.stats.late == 1
    candle = b.close_through(OPEN_UTC + timedelta(minutes=10))[0]
    # The 999.0 late tick must not appear in the second bar either.
    assert candle.high == 200.0


def test_out_of_order_ticks_inside_the_open_bar_are_still_accepted():
    b = builder()
    b.add(tick(3, 100.0))
    b.add(tick(1, 90.0))
    candle = b.close_through(OPEN_UTC + timedelta(minutes=5))[0]
    assert candle.low == 90.0
    assert candle.tick_count == 2
    assert b.stats.late == 0


def test_ticks_before_the_session_open_are_refused():
    b = builder()
    with pytest.raises(TickRejected) as caught:
        b.add(tick(-5, 100.0))
    assert caught.value.kind == "before_session"
    assert b.stats.before_session == 1


def test_volume_uses_the_cumulative_delta_when_available():
    b = builder()
    b.add(tick(0, 100.0, cumulative_volume=1_000))
    b.add(tick(2, 101.0, cumulative_volume=1_250))
    b.add(tick(4, 102.0, cumulative_volume=1_400))
    candle = b.close_through(OPEN_UTC + timedelta(minutes=5))[0]
    assert candle.volume == 400


def test_volume_sums_quantities_when_no_cumulative_total_is_published():
    b = builder()
    b.add(tick(0, 100.0, last_quantity=50))
    b.add(tick(2, 101.0, last_quantity=25))
    candle = b.close_through(OPEN_UTC + timedelta(minutes=5))[0]
    assert candle.volume == 75


def test_volume_is_none_when_the_feed_publishes_neither():
    b = builder()
    b.add(tick(0, 100.0))
    assert b.close_through(OPEN_UTC + timedelta(minutes=5))[0].volume is None


def test_mock_provenance_is_carried_onto_the_candle():
    b = builder()
    b.add(tick(0, 100.0))
    candle = b.close_through(OPEN_UTC + timedelta(minutes=5))[0]
    assert candle.provenance.is_mock is True
    assert candle.provenance.provider == "test"


def test_a_tick_for_another_symbol_is_a_programming_error():
    b = builder()
    foreign = Tick(symbol="NSE:BANKNIFTY", exchange_time=OPEN_UTC, last_price=100.0)
    with pytest.raises(ValueError, match="BANKNIFTY"):
        b.add(foreign)


def test_naive_timestamps_are_refused():
    b = builder()
    with pytest.raises(ValueError):
        b.bucket_open_for(datetime(2026, 10, 20, 3, 45))


def test_candle_close_time_matches_the_signal_contract_for_the_same_bar():
    # A 5m bar opening 09:15 IST closes 09:20 IST == 03:50Z, which is exactly
    # the bar_close_time the webhook derives from TradingView's {{time}}.
    from app.signals.timeframes import bar_close_from_open

    b = builder()
    b.add(tick(0, 100.0))
    candle = b.close_through(OPEN_UTC + timedelta(minutes=5))[0]
    assert candle.close_time == bar_close_from_open(OPEN_UTC, "5")
