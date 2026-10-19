from datetime import date, datetime, timedelta, timezone

import pytest

from app.market_data.contracts import (
    Candle,
    CandleSeries,
    OptionType,
    Provenance,
    Quote,
    Tick,
)
from app.market_data.health import FeedState, FeedWatchdog, ReconnectBackoff
from app.market_data.mock_provider import (
    RecordedMarketData,
    candle,
    index_instrument,
    option_instrument,
)
from app.market_data.protocols import (
    DataUnavailable,
    HistoricalCandleSource,
    InstrumentMaster,
    QuoteSource,
)

NOW = datetime(2026, 10, 20, 4, 40, tzinfo=timezone.utc)
LIVE = Provenance(provider="test", fetched_at=NOW)


def a_candle(**overrides) -> Candle:
    values = {
        "symbol": "NSE:NIFTY",
        "timeframe": "5m",
        "open_time": NOW,
        "close_time": NOW + timedelta(minutes=5),
        "open": 100.0,
        "high": 110.0,
        "low": 95.0,
        "close": 105.0,
    }
    values.update(overrides)
    return Candle(**values)


# --- provenance ---------------------------------------------------------------


def test_provenance_requires_an_aware_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        Provenance(provider="test", fetched_at=datetime(2026, 10, 20, 4, 40))


def test_mock_data_is_flagged_as_mock():
    instrument = index_instrument()
    assert instrument.provenance.is_mock is True
    assert instrument.provenance.provider == "mock"


# --- tick / candle invariants -------------------------------------------------


def test_tick_rejects_naive_time_and_non_positive_price():
    with pytest.raises(ValueError, match="timezone-aware"):
        Tick(symbol="X", exchange_time=datetime(2026, 1, 1), last_price=1.0)
    with pytest.raises(ValueError, match="positive"):
        Tick(symbol="X", exchange_time=NOW, last_price=0.0)


def test_candle_rejects_an_inverted_interval():
    with pytest.raises(ValueError, match="after open_time"):
        a_candle(close_time=NOW - timedelta(minutes=1))


@pytest.mark.parametrize(
    "overrides",
    [
        {"high": 100.0, "close": 105.0},  # high below close
        {"low": 106.0},  # low above open and close
    ],
)
def test_candle_rejects_inconsistent_ohlc(overrides):
    with pytest.raises(ValueError, match="inconsistent"):
        a_candle(**overrides)


def test_candle_body_and_range():
    bar = a_candle()
    assert bar.range == pytest.approx(15.0)
    assert bar.body == pytest.approx(5.0)


# --- quote --------------------------------------------------------------------


def test_two_sided_quote_derives_spread_and_mid():
    quote = Quote(symbol="OPT", quoted_at=NOW, bid=100.0, ask=102.0, provenance=LIVE)
    assert quote.is_two_sided
    assert quote.spread == pytest.approx(2.0)
    assert quote.mid == pytest.approx(101.0)
    assert quote.spread_fraction_of_mid == pytest.approx(2.0 / 101.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bid": None, "ask": 102.0},
        {"bid": 100.0, "ask": None},
        {"bid": 103.0, "ask": 102.0},  # crossed
        {"bid": 0.0, "ask": 102.0},
    ],
)
def test_a_quote_that_is_not_genuinely_two_sided_exposes_no_spread(kwargs):
    quote = Quote(symbol="OPT", quoted_at=NOW, **kwargs)
    assert not quote.is_two_sided
    assert quote.spread is None
    assert quote.mid is None


def test_quote_staleness_is_measured_in_both_directions():
    quote = Quote(symbol="OPT", quoted_at=NOW, bid=1.0, ask=2.0)
    assert not quote.is_stale(NOW + timedelta(seconds=2), timedelta(seconds=5))
    assert quote.is_stale(NOW + timedelta(seconds=30), timedelta(seconds=5))
    # A quote timestamped in the future is a clock or feed fault, not freshness.
    assert quote.is_stale(NOW - timedelta(seconds=30), timedelta(seconds=5))


# --- candle series ------------------------------------------------------------


def test_series_must_be_strictly_ascending():
    first = a_candle()
    with pytest.raises(ValueError, match="ascending"):
        CandleSeries(symbol="NSE:NIFTY", timeframe="5m", candles=(first, first))


def test_series_rejects_a_foreign_symbol_or_timeframe():
    with pytest.raises(ValueError, match="one symbol and one timeframe"):
        CandleSeries(symbol="NSE:BANKNIFTY", timeframe="5m", candles=(a_candle(),))


def test_series_reports_gaps_and_does_not_fill_them():
    bar = timedelta(minutes=5)
    first = a_candle()
    third = a_candle(open_time=NOW + 2 * bar, close_time=NOW + 3 * bar)
    series = CandleSeries(symbol="NSE:NIFTY", timeframe="5m", candles=(first, third))
    # The missing middle bar stays missing; it is reported, not invented.
    assert len(series) == 2
    assert series.gaps(bar) == ((first.close_time, third.open_time),)


def test_contiguous_series_has_no_gaps():
    bar = timedelta(minutes=5)
    series = CandleSeries(
        symbol="NSE:NIFTY",
        timeframe="5m",
        candles=(a_candle(), a_candle(open_time=NOW + bar, close_time=NOW + 2 * bar)),
    )
    assert series.gaps(bar) == ()


# --- mock provider ------------------------------------------------------------


def test_mock_provider_satisfies_the_protocols():
    provider = RecordedMarketData()
    assert isinstance(provider, InstrumentMaster)
    assert isinstance(provider, HistoricalCandleSource)
    assert isinstance(provider, QuoteSource)


def test_mock_provider_raises_rather_than_inventing_data():
    provider = RecordedMarketData()
    with pytest.raises(DataUnavailable, match="no instrument"):
        provider.resolve("NSE:NIFTY")
    with pytest.raises(DataUnavailable, match="no option contracts"):
        provider.option_expiries("NSE:NIFTY")


def test_mock_provider_serves_only_the_range_requested():
    bar = timedelta(minutes=5)
    instrument = index_instrument()
    provider = RecordedMarketData(instruments={instrument.symbol: instrument})
    provider.add_series(
        CandleSeries(
            symbol=instrument.symbol,
            timeframe="5m",
            candles=tuple(
                candle(
                    symbol=instrument.symbol,
                    timeframe="5m",
                    open_time=NOW + index * bar,
                    close_time=NOW + (index + 1) * bar,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                )
                for index in range(4)
            ),
        )
    )
    selected = provider.fetch_candles(instrument, "5m", NOW + bar, NOW + 3 * bar)
    assert len(selected) == 2
    assert selected.candles[0].open_time == NOW + bar


def test_mock_provider_lists_real_contracts_only():
    expiry = date(2026, 10, 27)
    call = option_instrument(expiry=expiry, strike=24_000.0, option_type=OptionType.CE)
    put = option_instrument(expiry=expiry, strike=24_000.0, option_type=OptionType.PE)
    provider = RecordedMarketData(
        instruments={call.symbol: call, put.symbol: put}
    )
    assert provider.option_expiries("NSE:NIFTY") == (expiry,)
    assert provider.option_contracts("NSE:NIFTY", expiry, OptionType.CE) == (call,)
    with pytest.raises(DataUnavailable):
        provider.option_contracts("NSE:NIFTY", date(2026, 11, 3))


def test_index_lookup_refuses_to_return_an_option():
    expiry = date(2026, 10, 27)
    call = option_instrument(expiry=expiry, strike=24_000.0, option_type=OptionType.CE)
    provider = RecordedMarketData(instruments={call.symbol: call})
    with pytest.raises(DataUnavailable, match="an option, not an index"):
        provider.index_instrument(call.symbol)


# --- feed health --------------------------------------------------------------


def test_a_feed_that_never_connected_is_not_usable():
    watchdog = FeedWatchdog()
    assert watchdog.state(NOW) is FeedState.NEVER_CONNECTED
    assert not watchdog.is_usable(NOW)


def test_a_connected_feed_with_recent_messages_is_healthy():
    watchdog = FeedWatchdog(silence_limit=timedelta(seconds=30))
    watchdog.on_connect(NOW)
    watchdog.on_message(NOW + timedelta(seconds=5))
    assert watchdog.is_usable(NOW + timedelta(seconds=10))


def test_an_open_but_silent_connection_is_not_usable():
    # The dangerous case: the socket is up and the price stopped moving.
    watchdog = FeedWatchdog(silence_limit=timedelta(seconds=30))
    watchdog.on_connect(NOW)
    assert watchdog.state(NOW + timedelta(minutes=5)) is FeedState.SILENT
    assert not watchdog.is_usable(NOW + timedelta(minutes=5))


def test_disconnection_is_reported_even_with_a_recent_message():
    watchdog = FeedWatchdog()
    watchdog.on_connect(NOW)
    watchdog.on_message(NOW)
    watchdog.on_disconnect()
    assert watchdog.state(NOW) is FeedState.DISCONNECTED


def test_an_out_of_order_message_cannot_refresh_a_stalling_feed():
    watchdog = FeedWatchdog(silence_limit=timedelta(seconds=30))
    watchdog.on_connect(NOW)
    watchdog.on_message(NOW + timedelta(seconds=60))
    watchdog.on_message(NOW + timedelta(seconds=10))
    assert watchdog.last_message_at == NOW + timedelta(seconds=60)


def test_watchdog_refuses_naive_timestamps():
    watchdog = FeedWatchdog()
    with pytest.raises(ValueError):
        watchdog.on_message(datetime(2026, 10, 20, 4, 40))


def test_backoff_grows_and_stays_capped():
    import random

    backoff = ReconnectBackoff(base_seconds=1.0, max_seconds=10.0, _random=random.Random(7))
    delays = [backoff.next_delay() for _ in range(12)]
    assert all(0.0 <= delay <= 10.0 for delay in delays)
    # Jitter is real, so the sequence must not be constant.
    assert len(set(delays)) > 1
    backoff.reset()
    assert backoff.attempt == 0
