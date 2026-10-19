"""FYERS adapter tests. Offline: transports are injected, no sockets."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import PROJECT_ROOT
from app.market_data.contracts import OptionType
from app.market_data.protocols import (
    CredentialsMissing,
    DataUnavailable,
    MarketDataError,
)
from app.market_data.providers import fyers

FIXTURES = PROJECT_ROOT / "examples" / "fixtures"
FETCHED_AT = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)
SESSION_OPEN = datetime(2026, 10, 19, 3, 45, tzinfo=timezone.utc)
CALL_TICKER = "NSE:NIFTY26O2924000CE"


@pytest.fixture
def master() -> fyers.FyersSymbolMaster:
    loaded = fyers.FyersSymbolMaster()
    loaded.load_json_bytes(
        (FIXTURES / "fyers-symbol-master-sample.json").read_bytes(), FETCHED_AT
    )
    return loaded


@pytest.fixture
def history_payload() -> dict:
    return json.loads((FIXTURES / "fyers-expired-option-history.json").read_text("utf-8"))


# --- documented limits ---------------------------------------------------------


def test_request_caps_match_the_documentation():
    for minutes in (1, 5, 60, 240):
        assert fyers.max_request_days(str(minutes)) == 100
    for resolution in ("1D", "1W", "1M"):
        assert fyers.max_request_days(resolution) == 366
    assert fyers.max_request_days("5S") == 30


@pytest.mark.parametrize("resolution", ["7", "0", "90", "2S", "1H", "weekly", ""])
def test_undocumented_resolutions_are_refused(resolution):
    with pytest.raises(MarketDataError):
        fyers.max_request_days(resolution)


def test_expired_history_start_dates_match_the_documentation():
    assert fyers.expired_history_start("NSE") == date(2018, 10, 10)
    assert fyers.expired_history_start("BSE") == date(2023, 8, 7)
    assert fyers.expired_history_start("mcx") == date(2018, 10, 11)
    with pytest.raises(DataUnavailable):
        fyers.expired_history_start("NCOM")


def test_expired_contract_support_is_documented_per_exchange():
    assert fyers.supports_expired_contracts("NSE")
    assert not fyers.supports_expired_contracts("NCOM")


def test_the_standard_plan_intraday_budget_is_recorded():
    assert fyers.plan_intraday_request_budget("standard") == 10
    assert fyers.plan_intraday_request_budget("prime") is None


def test_unverified_flags_are_honest():
    assert fyers.EXPIRY_UNIT_VERIFIED is False
    assert fyers.CANDLE_TIMEZONE_VERIFIED is False
    assert fyers.VERIFIED_AGAINST_LIVE_ACCOUNT is False


def test_symbol_master_urls_are_listed_and_unknown_segments_refused():
    urls = fyers.symbol_master_urls(["NSE_FO"])
    assert urls == {"NSE_FO": "https://public.fyers.in/sym_details/NSE_FO_sym_master.json"}
    with pytest.raises(DataUnavailable, match="no documented FYERS symbol master"):
        fyers.symbol_master_urls(["NSE_XYZ"])


# --- symbol master -------------------------------------------------------------


def test_symbol_master_loads_the_keyed_object(master):
    assert len(master) == 3
    assert master.fetched_at == FETCHED_AT


def test_index_resolves_through_an_alias(master):
    instrument = master.resolve("NSE:NIFTY")
    assert instrument.provider_key == "NSE:NIFTY50-INDEX"
    assert instrument.option_type is None
    # strikePrice is -1 on the index record; that is not a strike.
    assert instrument.strike is None
    assert instrument.extra["ex_token"] == 26000


def test_option_fields_come_from_the_record(master):
    call = master.resolve(CALL_TICKER)
    assert call.option_type is OptionType.CE
    assert call.strike == 24_000.0
    assert call.lot_size == 75
    assert call.tick_size == 0.05
    assert call.expiry == date(2026, 10, 29)
    assert call.underlying_symbol == "NIFTY"
    assert call.extra["qty_freeze"] == "1800"


def test_an_unknown_ticker_raises(master):
    with pytest.raises(DataUnavailable, match="not in the loaded FYERS symbol master"):
        master.resolve("NSE:NOSUCH25OCT100CE")


def test_inactive_records_are_excluded_from_active_only(master):
    active = {instrument.provider_key for instrument in master.active_only()}
    assert CALL_TICKER in active
    # The PE record carries tradeStatus 0.
    assert "NSE:NIFTY26O2924000PE" not in active


def test_querying_before_loading_raises():
    with pytest.raises(DataUnavailable, match="has not been loaded"):
        fyers.FyersSymbolMaster().resolve("NSE:NIFTY")


def test_refresh_without_a_transport_names_what_is_missing():
    with pytest.raises(CredentialsMissing, match="no HTTP transport"):
        fyers.FyersSymbolMaster().refresh()


def test_refresh_uses_the_injected_transport():
    blob = (FIXTURES / "fyers-symbol-master-sample.json").read_bytes()
    loaded = fyers.FyersSymbolMaster()
    assert loaded.refresh(transport=lambda url: blob, now=FETCHED_AT) == FETCHED_AT
    assert len(loaded) == 3


# --- malformed provider data ---------------------------------------------------


def test_a_non_object_symbol_master_is_rejected():
    with pytest.raises(MarketDataError, match="keyed by symTicker"):
        fyers.FyersSymbolMaster().load_mapping([], FETCHED_AT)


def test_a_non_object_entry_is_rejected():
    with pytest.raises(MarketDataError, match="expected an object"):
        fyers.FyersSymbolMaster().load_mapping({"NSE:X": "nope"}, FETCHED_AT)


def test_invalid_json_is_rejected():
    with pytest.raises(MarketDataError, match="not valid JSON"):
        fyers.FyersSymbolMaster().load_json_bytes(b"{nope", FETCHED_AT)


@pytest.mark.parametrize("value", ["", "soon", 0, -5, True, None])
def test_an_unparseable_expiry_raises(value):
    with pytest.raises(MarketDataError):
        fyers.expiry_to_date(value)


def test_expiry_accepts_seconds_milliseconds_and_iso_dates():
    seconds = int(datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc).timestamp())
    assert fyers.expiry_to_date(seconds) == date(2026, 10, 29)
    assert fyers.expiry_to_date(seconds * 1000) == date(2026, 10, 29)
    assert fyers.expiry_to_date("2026-10-29") == date(2026, 10, 29)


# --- history requests ----------------------------------------------------------


def test_history_requests_use_epoch_values_by_default():
    request = fyers.history_request(
        CALL_TICKER, "5", SESSION_OPEN, SESSION_OPEN + timedelta(hours=2)
    )
    assert request["date_format"] == 0
    assert request["range_from"].isdigit()
    assert request["symbol"] == CALL_TICKER


def test_range_to_is_pulled_back_to_the_previous_whole_minute():
    # The documentation advises this so a partial final candle is not returned.
    end = SESSION_OPEN + timedelta(hours=2)
    request = fyers.history_request(CALL_TICKER, "5", SESSION_OPEN, end)
    assert int(request["range_to"]) == int((end - timedelta(minutes=1)).timestamp())


def test_date_strings_can_be_requested_explicitly():
    request = fyers.history_request(
        CALL_TICKER, "1D", SESSION_OPEN, SESSION_OPEN + timedelta(days=5), use_epoch=False
    )
    assert request["date_format"] == 1
    assert request["range_from"] == "2026-10-19"


def test_a_span_beyond_the_documented_cap_is_refused_not_truncated():
    with pytest.raises(MarketDataError, match="exceeds the documented 100-day cap"):
        fyers.history_request(
            CALL_TICKER, "5", SESSION_OPEN, SESSION_OPEN + timedelta(days=120)
        )


def test_reversed_ranges_are_refused():
    with pytest.raises(ValueError, match="after range_to"):
        fyers.history_request(
            CALL_TICKER, "5", SESSION_OPEN, SESSION_OPEN - timedelta(hours=1)
        )


def test_greeks_are_only_offered_on_the_expired_endpoint():
    with pytest.raises(ValueError, match="expired F&O endpoint only"):
        fyers.history_request(
            CALL_TICKER,
            "5",
            SESSION_OPEN,
            SESSION_OPEN + timedelta(hours=1),
            include_greeks=True,
        )
    request = fyers.history_request(
        CALL_TICKER,
        "5",
        SESSION_OPEN,
        SESSION_OPEN + timedelta(hours=1),
        include_greeks=True,
        include_oi=True,
        expired=True,
    )
    assert request["include_greeks"] == 1 and request["include_oi"] == 1


# --- history response parsing --------------------------------------------------


def parse(payload: dict):
    return fyers.parse_history_response(
        payload,
        symbol="NSE:NIFTY-24000-CE",
        timeframe="5m",
        bar=timedelta(minutes=5),
        fetched_at=FETCHED_AT,
    )


def test_history_parses_by_column_name(history_payload):
    series, rows = parse(history_payload)
    assert len(series) == 3
    first = series.candles[0]
    assert (first.open, first.high, first.low, first.close) == (120.0, 122.5, 119.5, 121.0)
    assert first.open_time == SESSION_OPEN
    assert first.close_time == SESSION_OPEN + timedelta(minutes=5)
    assert first.volume == 45_000
    assert rows[0].implied_volatility == 11.87
    assert rows[0].greeks == {"delta": 0.52, "gamma": 0.0004, "theta": -8.1, "vega": 12.4}


def test_reordered_columns_are_handled(history_payload):
    # Column order is the provider's choice; indexes must never be assumed.
    columns = history_payload["columns"]
    order = [columns.index(name) for name in ("close", "timestamp", "high", "low", "open")]
    history_payload["columns"] = ["close", "timestamp", "high", "low", "open"]
    history_payload["candles"] = [
        [row[position] for position in order] for row in history_payload["candles"]
    ]
    series, _ = parse(history_payload)
    first = series.candles[0]
    assert (first.open, first.high, first.low, first.close) == (120.0, 122.5, 119.5, 121.0)
    assert first.volume is None  # volume was not among the columns


def test_null_open_interest_and_greeks_are_preserved(history_payload):
    _, rows = parse(history_payload)
    # The API neither carries values forward nor substitutes zero. Nor does this.
    assert rows[1].open_interest is None
    assert rows[1].implied_volatility is None
    assert rows[1].greeks == {"delta": None, "gamma": None, "theta": None, "vega": None}
    assert rows[2].open_interest == 1_512_000


def test_the_iv_column_alias_is_accepted(history_payload):
    history_payload["columns"] = [
        "implied_volatility" if name == "iv" else name for name in history_payload["columns"]
    ]
    _, rows = parse(history_payload)
    assert rows[0].implied_volatility == 11.87


def test_no_data_is_an_empty_answer_not_a_failure():
    series, rows = parse({"s": "no_data", "candles": []})
    assert len(series) == 0 and rows == ()


def test_an_error_status_raises_with_the_provider_message():
    with pytest.raises(DataUnavailable, match="invalid input"):
        parse({"s": "error", "code": -50, "message": "invalid input"})


def test_missing_columns_are_refused_rather_than_guessed():
    with pytest.raises(MarketDataError, match="no 'columns' array"):
        parse({"s": "ok", "candles": [[1, 2, 3, 4, 5]]})


def test_columns_missing_a_required_name_are_refused():
    with pytest.raises(MarketDataError, match="missing 'close'"):
        parse({"s": "ok", "columns": ["timestamp", "open", "high", "low"], "candles": []})


def test_a_non_list_candles_field_is_refused():
    with pytest.raises(MarketDataError, match="'candles' should be a list"):
        parse({"s": "ok", "columns": ["timestamp", "open", "high", "low", "close"], "candles": {}})


def test_a_non_list_candle_row_is_refused():
    with pytest.raises(MarketDataError, match="row should be a list"):
        parse(
            {
                "s": "ok",
                "columns": ["timestamp", "open", "high", "low", "close"],
                "candles": [{"timestamp": 1}],
            }
        )


def test_non_numeric_prices_are_refused():
    with pytest.raises(MarketDataError, match="non-numeric prices"):
        parse(
            {
                "s": "ok",
                "columns": ["timestamp", "open", "high", "low", "close"],
                "candles": [[int(SESSION_OPEN.timestamp()), "a", 2.0, 1.0, 1.5]],
            }
        )


def test_a_millisecond_timestamp_is_refused_rather_than_reinterpreted():
    with pytest.raises(MarketDataError, match="looks like milliseconds"):
        parse(
            {
                "s": "ok",
                "columns": ["timestamp", "open", "high", "low", "close"],
                "candles": [[int(SESSION_OPEN.timestamp() * 1000), 1.0, 2.0, 0.5, 1.5]],
            }
        )


def test_a_missing_timestamp_is_refused():
    with pytest.raises(MarketDataError, match="no timestamp"):
        parse(
            {
                "s": "ok",
                "columns": ["timestamp", "open", "high", "low", "close"],
                "candles": [[None, 1.0, 2.0, 0.5, 1.5]],
            }
        )


# --- fetching through an injected transport ------------------------------------


def test_fetching_without_a_transport_names_the_permission_needed(master):
    source = fyers.FyersHistoricalCandles()
    with pytest.raises(CredentialsMissing, match="Market Data permission"):
        source.fetch_candles(
            master.resolve(CALL_TICKER), "5m", SESSION_OPEN, SESSION_OPEN + timedelta(hours=1)
        )


def test_fetching_expired_data_uses_the_expired_transport(master, history_payload):
    seen: list[dict] = []

    def expired_transport(request: dict) -> dict:
        seen.append(request)
        return history_payload

    source = fyers.FyersHistoricalCandles(expired_transport=expired_transport)
    series = source.fetch_candles(
        master.resolve(CALL_TICKER),
        "5m",
        SESSION_OPEN,
        SESSION_OPEN + timedelta(hours=1),
        expired=True,
        include_oi=True,
        now=FETCHED_AT,
    )
    assert len(series) == 3
    assert seen[0]["resolution"] == "5"
    assert seen[0]["include_oi"] == 1
    assert series.candles[0].provenance.endpoint == "fyers.fno_historical_data"


def test_the_live_transport_is_not_used_for_expired_requests(master, history_payload):
    source = fyers.FyersHistoricalCandles(transport=lambda request: history_payload)
    with pytest.raises(CredentialsMissing, match="expired-F&O"):
        source.fetch_candles(
            master.resolve(CALL_TICKER),
            "5m",
            SESSION_OPEN,
            SESSION_OPEN + timedelta(hours=1),
            expired=True,
        )


def test_fetch_rows_keeps_the_analytics_columns(master, history_payload):
    source = fyers.FyersHistoricalCandles(expired_transport=lambda request: history_payload)
    rows = source.fetch_rows(
        master.resolve(CALL_TICKER),
        "5m",
        SESSION_OPEN,
        SESSION_OPEN + timedelta(hours=1),
        expired=True,
        include_oi=True,
        include_greeks=True,
        now=FETCHED_AT,
    )
    assert len(rows) == 3
    assert rows[0].greeks["delta"] == 0.52
    assert rows[1].greeks["delta"] is None


def test_engine_timeframes_map_to_documented_resolutions():
    assert fyers._to_resolution("5m") == "5"
    assert fyers._to_resolution("240m") == "240"
    assert fyers._to_resolution("5s") == "5S"
    assert fyers._to_resolution("1D") == "1D"


def test_no_history_start_is_claimed_for_live_contracts():
    # Only the expired-contract start dates are documented; inventing one here
    # would be a fabricated capability.
    assert fyers.FyersHistoricalCandles().earliest_available("5m") is None


def test_max_range_is_exposed_per_timeframe():
    source = fyers.FyersHistoricalCandles()
    assert source.max_range("5m") == 100
    assert source.max_range("1D") == 366
