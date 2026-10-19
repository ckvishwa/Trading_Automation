"""FYERS adapter: symbol master and historical research data.

Role: **historical research data** for chronological replay and walk-forward
evaluation, with Upstox as the primary live feed. Keeping the two roles in
separate adapters means a research-data outage cannot touch the live path, and a
disagreement between the two is detectable rather than invisible.

Everything here is offline-testable: transports are injected, and response
parsing is a pure function over the documented payload shape.

Documented facts, from FYERS API v3 documentation read on 2026-10-19. Nothing
has been confirmed against a live account.

**Symbol master** -- plain JSON over HTTPS at
`https://public.fyers.in/sym_details/{NSE_CM,NSE_FO,NSE_CD,NSE_COM,BSE_CM,BSE_FO,MCX_COM}_sym_master.json`,
an object keyed by `symTicker`. Each record carries `fyToken`, `isin`,
`exSymbol`, `symDetails`, `symTicker`, `exchange` and `segment` (integer codes),
`exSymName`, `exToken`, `exSeries`, `optType` (`CE`/`PE`, or `XX` for
non-options), `underSym`, `underFyTok`, `exInstType`, `minLotSize`, `tickSize`,
`tradingSession` (IST), `lastUpdate` (`YYYY-MM-DD`), `expiryDate` (a timestamp
string, derivatives only), `strikePrice`, `qtyFreeze`, `tradeStatus` (1 active),
`upperPrice`/`lowerPrice`, `previousClose`, `previousOi`, and more. The page
does **not** state the epoch base or unit of `expiryDate`, nor the update
frequency, nor whether the download needs authentication.

**Historical data** -- intraday resolutions 1, 2, 3, 5, 10, 15, 20, 30, 45, 60,
120, 180, 240 minutes capped at **100 days per request**; `1D`/`1W`/`1M` capped
at **366 days**. `date_format` 0 means `range_from`/`range_to` are epoch values,
1 means `yyyy-mm-dd`. The docs advise a `range_to` of the **previous minute** so
only completed candles come back. The app needs the Market Data permission; a
403 has been traced to app permissions rather than the token. Community reports
of short responses for date-only intraday ranges are treated here as a hazard:
`history_request` emits epoch values by default.

**Expired F&O** -- covers expired contracts only, via the SDK call
`fyers.fno_historical_data(data)`; the page states no HTTP path. Parameters
`symbol` (e.g. `NSE:NIFTY25MAR23000CE`), `resolution`, `date_format`,
`range_from`, `range_to`, optional `include_oi` and `include_greeks` (CE/PE
only, not futures). Resolutions add `5S`, limited to the last 30 trading days;
minute resolutions keep the 100-day cap. History starts 2018-10-10 (NSE),
2023-08-23 -- published as 2023-08-07 -- (BSE) and 2018-10-11 (MCX). The
Standard plan gets 2 years of daily candles and **10 intraday requests per
day**; Prime gets 7+ years. Responses carry `s`, `symbol`, `resolution`,
`schema_version`, `columns` and `candles`; **`columns` must be used instead of
fixed indexes**, OI and Greek values may be `null` per candle and are neither
carried forward nor zero-filled, `s: "no_data"` with an empty `candles` array is
a valid empty answer rather than a failure, and invalid input returns HTTP 422
with code `-50`. Timestamps are UNIX epoch **seconds** with no documented
timezone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from app.market_data.contracts import (
    Candle,
    CandleSeries,
    Instrument,
    OptionType,
    Provenance,
)
from app.market_data.protocols import (
    CredentialsMissing,
    DataUnavailable,
    MarketDataError,
)

PROVIDER_NAME = "fyers"
IST = ZoneInfo("Asia/Kolkata")

SYMBOL_MASTER_BASE = "https://public.fyers.in/sym_details/"
SYMBOL_MASTER_FILES = {
    "NSE_CM": SYMBOL_MASTER_BASE + "NSE_CM_sym_master.json",
    "NSE_FO": SYMBOL_MASTER_BASE + "NSE_FO_sym_master.json",
    "NSE_CD": SYMBOL_MASTER_BASE + "NSE_CD_sym_master.json",
    "NSE_COM": SYMBOL_MASTER_BASE + "NSE_COM_sym_master.json",
    "BSE_CM": SYMBOL_MASTER_BASE + "BSE_CM_sym_master.json",
    "BSE_FO": SYMBOL_MASTER_BASE + "BSE_FO_sym_master.json",
    "MCX_COM": SYMBOL_MASTER_BASE + "MCX_COM_sym_master.json",
}

INTRADAY_RESOLUTION_MINUTES = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120, 180, 240)
SECOND_RESOLUTIONS = ("5S",)
DAILY_PLUS_RESOLUTIONS = ("1D", "1W", "1M")

MAX_REQUEST_DAYS_INTRADAY = 100
MAX_REQUEST_DAYS_DAILY_PLUS = 366
MAX_TRADING_DAYS_SECONDS = 30

EXPIRED_HISTORY_START = {
    "NSE": date(2018, 10, 10),
    "BSE": date(2023, 8, 7),
    "MCX": date(2018, 10, 11),
}
STANDARD_PLAN_INTRADAY_REQUESTS_PER_DAY = 10

EXPIRY_UNIT_VERIFIED = False
CANDLE_TIMEZONE_VERIFIED = False
VERIFIED_AGAINST_LIVE_ACCOUNT = False


def _resolution_family(resolution: str) -> str:
    text = str(resolution).strip().upper()
    if text in DAILY_PLUS_RESOLUTIONS:
        return "daily_plus"
    if text in SECOND_RESOLUTIONS:
        return "seconds"
    try:
        minutes = int(text)
    except ValueError:
        raise MarketDataError(
            f"unsupported FYERS resolution {resolution!r}; documented values are "
            f"{INTRADAY_RESOLUTION_MINUTES}, {SECOND_RESOLUTIONS} and {DAILY_PLUS_RESOLUTIONS}"
        ) from None
    if minutes not in INTRADAY_RESOLUTION_MINUTES:
        raise MarketDataError(
            f"FYERS resolution {resolution!r} is not in the documented set "
            f"{INTRADAY_RESOLUTION_MINUTES}"
        )
    return "intraday"


def max_request_days(resolution: str) -> int:
    family = _resolution_family(resolution)
    if family == "daily_plus":
        return MAX_REQUEST_DAYS_DAILY_PLUS
    if family == "seconds":
        # Documented as the last 30 *trading* days, which is not a fixed number
        # of calendar days; the calendar-day figure is deliberately larger so a
        # caller never asks for more trading days than exist.
        return MAX_TRADING_DAYS_SECONDS
    return MAX_REQUEST_DAYS_INTRADAY


def expired_history_start(exchange: str) -> date:
    try:
        return EXPIRED_HISTORY_START[exchange.upper()]
    except KeyError:
        raise DataUnavailable(
            f"FYERS publishes no expired-contract history start for {exchange!r}"
        ) from None


def expiry_to_date(value: Any) -> date:
    """Convert `expiryDate` to a calendar date in IST.

    The unit is undocumented (see `EXPIRY_UNIT_VERIFIED`), so both seconds and
    milliseconds are handled, and anything outside a plausible range raises
    instead of silently landing near 1970.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise MarketDataError("empty FYERS expiryDate")
        try:
            return date.fromisoformat(text)
        except ValueError:
            try:
                value = int(text)
            except ValueError:
                raise MarketDataError(f"unparseable FYERS expiryDate {value!r}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarketDataError(f"unparseable FYERS expiryDate {value!r}")
    if value <= 0:
        raise MarketDataError(f"non-positive FYERS expiryDate {value!r}")
    seconds = value / 1000 if value > 10**11 else float(value)
    return datetime.fromtimestamp(seconds, tz=IST).date()


# --------------------------------------------------------------------------
# Symbol master
# --------------------------------------------------------------------------

# Engine symbol -> FYERS symTicker. The symbol-master page does not document the
# ticker format or show a NIFTY index record, so these are expectations to check
# against a downloaded file. `resolve` raises rather than guessing.
DEFAULT_SYMBOL_ALIASES = {
    "NSE:NIFTY": "NSE:NIFTY50-INDEX",
    "NSE:BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "BSE:SENSEX": "BSE:SENSEX-INDEX",
}


class FyersSymbolMaster:
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        symbol_aliases: dict[str, str] | None = None,
        source_url: str = SYMBOL_MASTER_FILES["NSE_FO"],
    ) -> None:
        self._aliases = dict(symbol_aliases or DEFAULT_SYMBOL_ALIASES)
        self._source_url = source_url
        self._by_ticker: dict[str, Instrument] = {}
        self._fetched_at: datetime | None = None

    def load_mapping(self, payload: dict, fetched_at: datetime) -> int:
        """Load the symbol master, which is an object keyed by `symTicker`."""
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        if not isinstance(payload, dict):
            raise MarketDataError(
                "FYERS symbol master should be a JSON object keyed by symTicker, "
                f"got {type(payload).__name__}"
            )
        provenance = Provenance(
            provider=PROVIDER_NAME,
            fetched_at=fetched_at,
            endpoint=self._source_url,
            is_mock=False,
        )
        loaded: dict[str, Instrument] = {}
        for ticker, record in payload.items():
            if not isinstance(record, dict):
                raise MarketDataError(
                    f"FYERS symbol master entry {ticker!r} is "
                    f"{type(record).__name__}, expected an object"
                )
            loaded[str(ticker)] = self._to_instrument(str(ticker), record, provenance)
        self._by_ticker = loaded
        self._fetched_at = fetched_at
        return len(loaded)

    def load_json_bytes(self, blob: bytes, fetched_at: datetime) -> int:
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise MarketDataError(f"FYERS symbol master is not valid JSON: {exc}") from exc
        return self.load_mapping(payload, fetched_at)

    def refresh(
        self, transport: Callable[[str], bytes] | None = None, now: datetime | None = None
    ) -> datetime:
        if transport is None:
            raise CredentialsMissing(
                "no HTTP transport supplied for the FYERS symbol master. Pass "
                f"transport=... or load bytes directly. Source: {self._source_url}"
            )
        fetched_at = now or datetime.now(tz=timezone.utc)
        self.load_json_bytes(transport(self._source_url), fetched_at)
        return fetched_at

    def _to_instrument(
        self, ticker: str, record: dict, provenance: Provenance
    ) -> Instrument:
        option_type: OptionType | None = None
        raw_option = str(record.get("optType", "XX")).upper()
        if raw_option in ("CE", "PE"):
            option_type = OptionType(raw_option)

        expiry: date | None = None
        if record.get("expiryDate") not in (None, "", 0, "0"):
            expiry = expiry_to_date(record["expiryDate"])

        lot_size = record.get("minLotSize")
        tick_size = record.get("tickSize")
        strike = record.get("strikePrice")
        exchange_name = record.get("exchangeName")

        return Instrument(
            symbol=f"{PROVIDER_NAME}:{ticker}",
            provider_key=ticker,
            exchange=str(exchange_name) if exchange_name else None,
            segment=str(record.get("segment")) if record.get("segment") is not None else None,
            lot_size=int(lot_size) if lot_size not in (None, "") else None,
            tick_size=float(tick_size) if tick_size not in (None, "") else None,
            expiry=expiry,
            # The page documents strikePrice on every record; 0 on a non-option
            # is not a strike, so it is dropped rather than stored as one.
            strike=float(strike) if strike not in (None, "") and float(strike) > 0 else None,
            option_type=option_type,
            underlying_symbol=record.get("underSym"),
            provenance=provenance,
            extra={
                "fy_token": record.get("fyToken"),
                "ex_symbol": record.get("exSymbol"),
                "ex_token": record.get("exToken"),
                "ex_inst_type": record.get("exInstType"),
                "trading_session": record.get("tradingSession"),
                "trade_status": record.get("tradeStatus"),
                "qty_freeze": record.get("qtyFreeze"),
                "last_update": record.get("lastUpdate"),
                "under_fy_token": record.get("underFyTok"),
            },
        )

    def __len__(self) -> int:
        return len(self._by_ticker)

    @property
    def fetched_at(self) -> datetime | None:
        return self._fetched_at

    def _loaded(self) -> None:
        if not self._by_ticker:
            raise DataUnavailable(
                "FYERS symbol master has not been loaded; call refresh() or "
                "load_json_bytes() first"
            )

    def resolve(self, symbol: str) -> Instrument:
        self._loaded()
        ticker = self._aliases.get(symbol.upper(), symbol)
        try:
            return self._by_ticker[ticker]
        except KeyError:
            raise DataUnavailable(
                f"{symbol!r} maps to symTicker {ticker!r}, which is not in the "
                "loaded FYERS symbol master; check the alias against the file"
            ) from None

    def active_only(self) -> tuple[Instrument, ...]:
        """Records flagged `tradeStatus == 1`. Inactive rows are not tradable."""
        self._loaded()
        return tuple(
            instrument
            for instrument in self._by_ticker.values()
            if instrument.extra.get("trade_status") == 1
        )


# --------------------------------------------------------------------------
# Historical data
# --------------------------------------------------------------------------


def history_request(
    symbol: str,
    resolution: str,
    range_from: datetime,
    range_to: datetime,
    *,
    use_epoch: bool = True,
    include_oi: bool = False,
    include_greeks: bool = False,
    expired: bool = False,
) -> dict:
    """Build a history request payload.

    Epoch values are the default because date-only strings have been reported to
    return far less than the requested intraday range. `range_to` is also pulled
    back to the previous whole minute, as the documentation advises, so a partial
    final candle is never returned.
    """
    family = _resolution_family(resolution)
    if range_from > range_to:
        raise ValueError(f"range_from {range_from} is after range_to {range_to}")
    if include_greeks and not expired:
        raise ValueError(
            "include_greeks is documented for the expired F&O endpoint only"
        )

    span_days = (range_to - range_from).days
    cap = max_request_days(resolution)
    if span_days > cap:
        raise MarketDataError(
            f"{span_days} days exceeds the documented {cap}-day cap for FYERS "
            f"resolution {resolution!r}; page the request instead of truncating it"
        )

    completed_to = range_to.replace(second=0, microsecond=0) - timedelta(minutes=1)
    if completed_to < range_from:
        completed_to = range_from

    payload: dict[str, Any] = {
        "symbol": symbol,
        "resolution": str(resolution).upper() if family != "intraday" else str(resolution),
        "date_format": 0 if use_epoch else 1,
    }
    if use_epoch:
        payload["range_from"] = str(int(range_from.timestamp()))
        payload["range_to"] = str(int(completed_to.timestamp()))
    else:
        payload["range_from"] = range_from.date().isoformat()
        payload["range_to"] = completed_to.date().isoformat()
    if include_oi:
        payload["include_oi"] = 1
    if include_greeks:
        payload["include_greeks"] = 1
    return payload


@dataclass(frozen=True)
class HistoryRow:
    """One candle plus the optional analytics columns, nulls preserved."""

    candle: Candle
    open_interest: int | None = None
    implied_volatility: float | None = None
    greeks: dict[str, float | None] | None = None


def parse_history_response(
    payload: dict,
    *,
    symbol: str,
    timeframe: str,
    bar: timedelta,
    fetched_at: datetime,
    endpoint: str = "fyers.history",
) -> tuple[CandleSeries, tuple[HistoryRow, ...]]:
    """Parse a history or expired-F&O response into engine candles.

    Column names come from the response's own `columns` array, never from fixed
    indexes: the documentation says so explicitly, and its own example labels the
    IV column `iv` while the field list calls it `implied_volatility`.
    """
    status = payload.get("s")
    if status == "no_data":
        # A valid empty answer, not a failure.
        return CandleSeries(symbol=symbol, timeframe=timeframe, candles=()), ()
    if status != "ok":
        raise DataUnavailable(
            f"FYERS history returned s={status!r}: "
            f"{payload.get('message') or payload.get('code') or payload}"
        )

    columns = payload.get("columns")
    candles = payload.get("candles")
    if not isinstance(columns, list) or not columns:
        raise MarketDataError(
            "FYERS history response has no 'columns' array; indexes must not be "
            "assumed because the column set depends on include_oi/include_greeks"
        )
    if not isinstance(candles, list):
        raise MarketDataError(
            f"FYERS history 'candles' should be a list, got {type(candles).__name__}"
        )

    index = {str(name): position for position, name in enumerate(columns)}
    for required in ("timestamp", "open", "high", "low", "close"):
        if required not in index:
            raise MarketDataError(
                f"FYERS history columns are missing {required!r}: {columns}"
            )
    iv_column = "implied_volatility" if "implied_volatility" in index else "iv"

    provenance = Provenance(
        provider=PROVIDER_NAME, fetched_at=fetched_at, endpoint=endpoint, is_mock=False
    )

    def value(row: list, name: str) -> Any:
        position = index.get(name)
        if position is None or position >= len(row):
            return None
        return row[position]

    rows: list[HistoryRow] = []
    for row in candles:
        if not isinstance(row, list):
            raise MarketDataError(
                f"FYERS candle row should be a list, got {type(row).__name__}: {row!r}"
            )
        raw_timestamp = value(row, "timestamp")
        if raw_timestamp in (None, ""):
            raise MarketDataError(f"FYERS candle row has no timestamp: {row!r}")
        # Documented as UNIX epoch seconds; milliseconds are rejected rather
        # than silently reinterpreted.
        seconds = float(raw_timestamp)
        if seconds > 10**11:
            raise MarketDataError(
                f"FYERS candle timestamp {raw_timestamp!r} looks like milliseconds; "
                "the API documents epoch seconds"
            )
        open_time = datetime.fromtimestamp(seconds, tz=timezone.utc)

        try:
            prices = {name: float(value(row, name)) for name in ("open", "high", "low", "close")}
        except (TypeError, ValueError) as exc:
            raise MarketDataError(f"FYERS candle row has non-numeric prices: {row!r}") from exc

        volume = value(row, "volume")
        open_interest = value(row, "open_interest")
        implied_volatility = value(row, iv_column)
        greek_values = {
            name: (None if value(row, name) is None else float(value(row, name)))
            for name in ("delta", "gamma", "theta", "vega")
            if name in index
        }

        candle = Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=open_time,
            close_time=open_time + bar,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            volume=int(volume) if volume not in (None, "") else None,
            # Nulls are preserved: the API neither carries values forward nor
            # substitutes zero, and neither does this parser.
            open_interest=int(open_interest) if open_interest not in (None, "") else None,
            provenance=provenance,
        )
        rows.append(
            HistoryRow(
                candle=candle,
                open_interest=candle.open_interest,
                implied_volatility=(
                    float(implied_volatility) if implied_volatility not in (None, "") else None
                ),
                greeks=greek_values or None,
            )
        )

    series = CandleSeries(
        symbol=symbol,
        timeframe=timeframe,
        candles=tuple(row.candle for row in rows),
    )
    return series, tuple(rows)


class FyersHistoricalCandles:
    """Historical candles over an injected transport.

    The transport is a callable taking the request payload and returning the
    decoded response, which is what the SDK's `history` / `fno_historical_data`
    calls already are. Without one, nothing is fetched and the failure says so.
    """

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        transport: Callable[[dict], dict] | None = None,
        *,
        expired_transport: Callable[[dict], dict] | None = None,
    ) -> None:
        self._transport = transport
        self._expired_transport = expired_transport

    def max_range(self, timeframe: str) -> int:
        return max_request_days(_to_resolution(timeframe))

    def earliest_available(self, timeframe: str) -> date | None:
        # Live-contract history depth is not documented per resolution, only the
        # expired-contract start dates are. Claiming a date here would invent one.
        return None

    def fetch_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        expired: bool = False,
        include_oi: bool = False,
        include_greeks: bool = False,
        now: datetime | None = None,
    ) -> CandleSeries:
        transport = self._expired_transport if expired else self._transport
        if transport is None:
            raise CredentialsMissing(
                f"no FYERS {'expired-F&O' if expired else 'history'} transport "
                "supplied. The app needs the Market Data permission (historical "
                "data, market depth, quotes); a 403 here is usually permissions, "
                "not the token."
            )
        resolution = _to_resolution(timeframe)
        request = history_request(
            instrument.provider_key,
            resolution,
            start,
            end,
            include_oi=include_oi,
            include_greeks=include_greeks,
            expired=expired,
        )
        response = transport(request)
        series, _ = parse_history_response(
            response,
            symbol=instrument.symbol,
            timeframe=timeframe,
            bar=_bar_length(timeframe),
            fetched_at=now or datetime.now(tz=timezone.utc),
            endpoint="fyers.fno_historical_data" if expired else "fyers.history",
        )
        return series

    def fetch_rows(self, *args, **kwargs) -> tuple[HistoryRow, ...]:
        """As `fetch_candles`, keeping OI, IV and Greeks alongside each candle."""
        transport = self._expired_transport if kwargs.get("expired") else self._transport
        if transport is None:
            raise CredentialsMissing("no FYERS transport supplied")
        instrument, timeframe, start, end = args
        request = history_request(
            instrument.provider_key,
            _to_resolution(timeframe),
            start,
            end,
            include_oi=kwargs.get("include_oi", False),
            include_greeks=kwargs.get("include_greeks", False),
            expired=kwargs.get("expired", False),
        )
        _, rows = parse_history_response(
            transport(request),
            symbol=instrument.symbol,
            timeframe=timeframe,
            bar=_bar_length(timeframe),
            fetched_at=kwargs.get("now") or datetime.now(tz=timezone.utc),
        )
        return rows


def _to_resolution(timeframe: str) -> str:
    """Canonical engine timeframe ("5m", "1D") -> FYERS resolution ("5", "1D")."""
    text = str(timeframe).strip()
    if text.endswith("m") and text[:-1].isdigit():
        return text[:-1]
    if text.endswith("s") and text[:-1].isdigit():
        return f"{text[:-1]}S"
    return text.upper()


def _bar_length(timeframe: str) -> timedelta:
    from app.signals.timeframes import timeframe_duration

    return timeframe_duration(timeframe)


def supports_expired_contracts(exchange: str = "NSE") -> bool:
    """Whether expired-contract history is documented for an exchange.

    Documented support is not the same as retrievable data for a given contract:
    the API can still answer `no_data`, and the engine must keep underlying
    directional evaluation separate from option-strategy P&L.
    """
    return exchange.upper() in EXPIRED_HISTORY_START


def plan_intraday_request_budget(plan: str) -> int | None:
    """Documented intraday request budget per day. None where unstated."""
    return STANDARD_PLAN_INTRADAY_REQUESTS_PER_DAY if plan.lower() == "standard" else None


def symbol_master_urls(segments: Iterable[str] | None = None) -> dict[str, str]:
    if segments is None:
        return dict(SYMBOL_MASTER_FILES)
    chosen = {}
    for segment in segments:
        key = segment.upper()
        if key not in SYMBOL_MASTER_FILES:
            raise DataUnavailable(
                f"no documented FYERS symbol master for segment {segment!r}; "
                f"known: {sorted(SYMBOL_MASTER_FILES)}"
            )
        chosen[key] = SYMBOL_MASTER_FILES[key]
    return chosen
