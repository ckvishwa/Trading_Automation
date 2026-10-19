"""Upstox adapter: instrument master, feed subscription, message normalization.

Primary live-data candidate. Everything here is **offline-testable**: the HTTP
and websocket transports are injected, so the instrument master is parsed from
bytes and feed messages are normalized from decoded dictionaries. No test in
this repository opens a socket.

Documented facts this module encodes, read from Upstox's official v3
documentation on 2026-10-19. None of it has been confirmed against a live
account; `verified_against_live_account` is False throughout.

**Rate limits** (per user, per API group; the page says "per-API, per-user"):
order placement 10/s (50/s for SEBI-registered algos), other standard APIs
including historical candles 50/s, and 500/min and 2000/30min for both. TOTP
login 1/s, 10/min, 60/30min. The page states no daily limit, no HTTP status for
a breach, no error code and no response headers -- only that exceeding the
limits "might result in temporary suspension of access". Backoff is therefore
blind and must be conservative.

**Instrument files** (gzipped JSON, CDN; CSV is deprecated):
`https://assets.upstox.com/market-quote/instruments/exchange/{complete,NSE,BSE,MCX,global,MTF,NSE_MIS,BSE_MIS,suspended-instrument,mf-instruments}.json.gz`,
refreshed daily around 06:00, intraday only rarely. The page does not say
whether the URLs require authentication. Records carry `instrument_key`,
`exchange_token`, `trading_symbol`, `name`, `segment`, `exchange`,
`instrument_type`, and for tradables `lot_size`, `tick_size`,
`freeze_quantity`; derivatives add `expiry`, `strike_price`, `underlying_key`,
`underlying_symbol`, `underlying_type`, `minimum_lot`, `weekly`. **JSON has no
`option_type` field: CE/PE live in `instrument_type`.** `expiry` is a 13-digit
value that the samples show as epoch milliseconds; the page types it only as
"date" and never states the unit, so `EXPIRY_UNIT_VERIFIED` is False.

**Instrument search** (`GET /v2/instruments/search`, bearer token): `query`
<= 50 chars, `exchanges`, `segments`, `instrument_types`, `expiry`,
`atm_offset`, `page_number` from 1, `records` <= 30. Exchange tokens are not
searchable; the instrument files are the lookup for those.

**Market data feed V3**: `wss://.../feed/market-data-feed` with
`Authorization: Bearer <token>`, answered by an HTTP 302 the client must
follow. Payloads are Protobuf per
`https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto`;
requests are binary frames carrying JSON of the shape
`{"guid", "method": "sub"|"unsub"|"change_mode", "data": {"mode", "instrumentKeys"}}`.
Modes `ltpc`, `option_greeks`, `full`, `full_d30`. Timestamps (`ltt`,
`currentTs`, `updatedTime`) are millisecond-epoch **strings** and the page does
not state a timezone, so they are read as UTC instants and that reading is
recorded as unverified. The feed's own OHLC covers only a few intervals (`1d`,
`I1`), which is why this engine builds its own candles from ticks.
"""

from __future__ import annotations

import gzip
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

from app.market_data.contracts import (
    Instrument,
    OptionType,
    Provenance,
    Quote,
    Tick,
)
from app.market_data.protocols import (
    CredentialsMissing,
    DataUnavailable,
    MarketDataError,
    RateLimited,
)

PROVIDER_NAME = "upstox"
IST = ZoneInfo("Asia/Kolkata")

INSTRUMENTS_BASE = "https://assets.upstox.com/market-quote/instruments/exchange/"
INSTRUMENT_FILES = {
    "complete": INSTRUMENTS_BASE + "complete.json.gz",
    "NSE": INSTRUMENTS_BASE + "NSE.json.gz",
    "BSE": INSTRUMENTS_BASE + "BSE.json.gz",
    "MCX": INSTRUMENTS_BASE + "MCX.json.gz",
    "global": INSTRUMENTS_BASE + "global.json.gz",
    "MTF": INSTRUMENTS_BASE + "MTF.json.gz",
    "NSE_MIS": INSTRUMENTS_BASE + "NSE_MIS.json.gz",
    "BSE_MIS": INSTRUMENTS_BASE + "BSE_MIS.json.gz",
    "suspended": INSTRUMENTS_BASE + "suspended-instrument.json.gz",
    "mutual_funds": INSTRUMENTS_BASE + "mf-instruments.json.gz",
}
INSTRUMENT_SEARCH_PATH = "/v2/instruments/search"
HISTORICAL_CANDLE_PATH = (
    "/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
)
FEED_PATH = "/feed/market-data-feed"
FEED_PROTO_URL = "https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto"

# Documented maximum request span in days, by interval family.
MAX_REQUEST_DAYS = {
    "minutes_upto_15": 31,
    "minutes_above_15": 92,
    "hours": 92,
    "days": 3653,
}
EARLIEST_INTRADAY = date(2022, 1, 1)
EARLIEST_DAILY = date(2000, 1, 1)

# The documentation types `expiry` as "date" and shows 13-digit values. Treating
# them as epoch milliseconds is an inference, flagged so it is not mistaken for
# a documented fact.
EXPIRY_UNIT_VERIFIED = False
FEED_TIMEZONE_VERIFIED = False
VERIFIED_AGAINST_LIVE_ACCOUNT = False


class FeedMode(StrEnum):
    LTPC = "ltpc"
    OPTION_GREEKS = "option_greeks"
    FULL = "full"
    FULL_D30 = "full_d30"


# mode -> (individual cap, combined cap, requires Upstox Plus)
FEED_MODE_LIMITS: dict[FeedMode, tuple[int, int, bool]] = {
    FeedMode.LTPC: (5000, 2000, False),
    FeedMode.OPTION_GREEKS: (3000, 2000, False),
    FeedMode.FULL: (2000, 1500, False),
    FeedMode.FULL_D30: (50, 1500, True),
}
MAX_CONNECTIONS = {"normal": 2, "plus": 5}


def _require(record: dict, key: str, kind: type | tuple[type, ...]) -> Any:
    """Read a required field, or fail naming the field and the record.

    Provider payloads change without notice. A missing or wrongly typed field is
    reported rather than defaulted, because a silently defaulted lot size or
    strike is a wrong trade waiting to happen.
    """
    if key not in record:
        raise MarketDataError(
            f"Upstox instrument record is missing {key!r}: "
            f"{json.dumps(record, default=str)[:200]}"
        )
    value = record[key]
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise MarketDataError(
            f"Upstox instrument field {key!r} should be {kind}, got {type(value).__name__} "
            f"({value!r})"
        )
    return value


def expiry_to_date(value: Any) -> date:
    """Convert an instrument record's `expiry` to a calendar date in IST.

    The JSON files carry epoch milliseconds (an inference -- see
    `EXPIRY_UNIT_VERIFIED`); the deprecated CSV carries `yyyy-MM-dd`. Expiry is
    a date *in the exchange's timezone*, so the instant is converted to IST
    before the date is taken: at 23:59:59.000+05:30 a naive UTC reading lands on
    the previous day.
    """
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            try:
                value = int(value)
            except ValueError:
                raise MarketDataError(f"unparseable Upstox expiry {value!r}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarketDataError(f"unparseable Upstox expiry {value!r}")
    if value <= 0:
        raise MarketDataError(f"non-positive Upstox expiry {value!r}")
    # 13 digits is milliseconds; 10 is seconds. Guarding both ways means a
    # schema change shifts the failure into the open rather than into 1970.
    seconds = value / 1000 if value > 10**11 else float(value)
    return datetime.fromtimestamp(seconds, tz=IST).date()


def epoch_millis_to_utc(value: Any) -> datetime:
    """Feed timestamps arrive as millisecond-epoch strings."""
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            raise MarketDataError(f"unparseable Upstox timestamp {value!r}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarketDataError(f"unparseable Upstox timestamp {value!r}")
    if value <= 0:
        raise MarketDataError(f"non-positive Upstox timestamp {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


# --------------------------------------------------------------------------
# Instrument master
# --------------------------------------------------------------------------

# Engine symbol -> (segment, trading_symbol). The documentation's index example
# is a BSE index, so NIFTY's own record was never shown; this mapping is an
# expectation to be checked against a downloaded file, not a documented fact.
# `resolve` raises rather than guessing when a mapping does not match.
DEFAULT_SYMBOL_ALIASES: dict[str, tuple[str, str]] = {
    "NSE:NIFTY": ("NSE_INDEX", "Nifty 50"),
    "NSE:BANKNIFTY": ("NSE_INDEX", "Nifty Bank"),
    "BSE:SENSEX": ("BSE_INDEX", "SENSEX"),
}


class UpstoxInstrumentMaster:
    """Parsed instrument master, queried by engine symbol or option criteria."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        symbol_aliases: dict[str, tuple[str, str]] | None = None,
        source_url: str = INSTRUMENT_FILES["complete"],
    ) -> None:
        self._aliases = dict(symbol_aliases or DEFAULT_SYMBOL_ALIASES)
        self._source_url = source_url
        self._by_key: dict[str, Instrument] = {}
        self._by_segment_symbol: dict[tuple[str, str], Instrument] = {}
        self._fetched_at: datetime | None = None

    # --- loading ----------------------------------------------------------

    def load_records(self, records: Iterable[dict], fetched_at: datetime) -> int:
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        provenance = Provenance(
            provider=PROVIDER_NAME,
            fetched_at=fetched_at,
            endpoint=self._source_url,
            is_mock=False,
        )
        by_key: dict[str, Instrument] = {}
        by_segment_symbol: dict[tuple[str, str], Instrument] = {}
        for record in records:
            instrument = self._to_instrument(record, provenance)
            by_key[instrument.provider_key] = instrument
            trading_symbol = str(instrument.extra["trading_symbol"])
            by_segment_symbol[(instrument.segment or "", trading_symbol)] = instrument
        self._by_key = by_key
        self._by_segment_symbol = by_segment_symbol
        self._fetched_at = fetched_at
        return len(by_key)

    def load_gz_bytes(self, blob: bytes, fetched_at: datetime) -> int:
        try:
            payload = json.loads(gzip.decompress(blob))
        except (OSError, json.JSONDecodeError) as exc:
            raise MarketDataError(f"Upstox instrument file is not gzipped JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise MarketDataError(
                f"Upstox instrument file should hold a JSON array, got {type(payload).__name__}"
            )
        return self.load_records(payload, fetched_at)

    def load_gz_path(self, path: Path, fetched_at: datetime) -> int:
        return self.load_gz_bytes(Path(path).read_bytes(), fetched_at)

    def refresh(
        self, transport: Callable[[str], bytes] | None = None, now: datetime | None = None
    ) -> datetime:
        """Download and reload the instrument master.

        `transport` is injected so this is testable and so the project does not
        acquire an HTTP client it does not otherwise need. Without one, nothing
        is fetched and the failure says so.
        """
        if transport is None:
            raise CredentialsMissing(
                "no HTTP transport supplied for the Upstox instrument file. Pass "
                f"transport=... or load a file with load_gz_path(). Source: {self._source_url}"
            )
        fetched_at = now or datetime.now(tz=timezone.utc)
        self.load_gz_bytes(transport(self._source_url), fetched_at)
        return fetched_at

    def _to_instrument(self, record: dict, provenance: Provenance) -> Instrument:
        segment = _require(record, "segment", str)
        trading_symbol = _require(record, "trading_symbol", str)
        instrument_type = _require(record, "instrument_type", str)
        provider_key = _require(record, "instrument_key", str)

        option_type: OptionType | None = None
        if instrument_type in ("CE", "PE"):
            # JSON has no option_type field: CE/PE arrive in instrument_type.
            option_type = OptionType(instrument_type)

        expiry: date | None = None
        if record.get("expiry") not in (None, "", 0):
            expiry = expiry_to_date(record["expiry"])

        lot_size = record.get("lot_size")
        tick_size = record.get("tick_size")
        strike = record.get("strike_price")

        return Instrument(
            symbol=f"{PROVIDER_NAME}:{provider_key}",
            provider_key=provider_key,
            exchange=record.get("exchange"),
            segment=segment,
            lot_size=int(lot_size) if lot_size not in (None, "") else None,
            tick_size=float(tick_size) if tick_size not in (None, "") else None,
            expiry=expiry,
            strike=float(strike) if strike not in (None, "") else None,
            option_type=option_type,
            underlying_symbol=record.get("underlying_symbol"),
            provenance=provenance,
            extra={
                "trading_symbol": trading_symbol,
                "instrument_type": instrument_type,
                "name": record.get("name"),
                "exchange_token": record.get("exchange_token"),
                "freeze_quantity": record.get("freeze_quantity"),
                "minimum_lot": record.get("minimum_lot"),
                "underlying_key": record.get("underlying_key"),
                "underlying_type": record.get("underlying_type"),
                "weekly": record.get("weekly"),
            },
        )

    # --- querying ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._by_key)

    @property
    def fetched_at(self) -> datetime | None:
        return self._fetched_at

    def _loaded(self) -> None:
        if not self._by_key:
            raise DataUnavailable(
                "Upstox instrument master has not been loaded; call refresh() or "
                "load_gz_path() first"
            )

    def by_provider_key(self, provider_key: str) -> Instrument:
        self._loaded()
        try:
            return self._by_key[provider_key]
        except KeyError:
            raise DataUnavailable(f"no Upstox instrument with key {provider_key!r}") from None

    def resolve(self, symbol: str) -> Instrument:
        """Resolve an engine symbol such as `NSE:NIFTY`."""
        self._loaded()
        alias = self._aliases.get(symbol.upper())
        if alias is None:
            raise DataUnavailable(
                f"no Upstox alias configured for {symbol!r}; add one rather than "
                "letting the engine construct a provider key"
            )
        try:
            return self._by_segment_symbol[alias]
        except KeyError:
            raise DataUnavailable(
                f"{symbol!r} maps to segment={alias[0]!r} trading_symbol={alias[1]!r}, "
                "which is not in the loaded instrument master. Check the alias "
                "against the downloaded file; the engine will not guess a key."
            ) from None

    def index_instrument(self, symbol: str) -> Instrument:
        instrument = self.resolve(symbol)
        if instrument.is_option or instrument.extra.get("instrument_type") != "INDEX":
            raise DataUnavailable(
                f"{symbol!r} resolves to instrument_type="
                f"{instrument.extra.get('instrument_type')!r}, not INDEX"
            )
        return instrument

    def _options_for(self, underlying_symbol: str) -> list[Instrument]:
        self._loaded()
        wanted = underlying_symbol.upper()
        alias = self._aliases.get(wanted)
        underlying_names = {wanted, wanted.split(":")[-1]}
        if alias is not None:
            underlying_names.add(alias[1].upper())
        return [
            instrument
            for instrument in self._by_key.values()
            if instrument.is_option
            and (instrument.underlying_symbol or "").upper() in underlying_names
        ]

    def option_expiries(self, underlying_symbol: str) -> tuple[date, ...]:
        found = {
            instrument.expiry
            for instrument in self._options_for(underlying_symbol)
            if instrument.expiry is not None
        }
        if not found:
            raise DataUnavailable(
                f"no Upstox option contracts for underlying {underlying_symbol!r}"
            )
        return tuple(sorted(found))

    def option_contracts(
        self,
        underlying_symbol: str,
        expiry: date,
        option_type: OptionType | None = None,
    ) -> tuple[Instrument, ...]:
        found = tuple(
            instrument
            for instrument in self._options_for(underlying_symbol)
            if instrument.expiry == expiry
            and (option_type is None or instrument.option_type is option_type)
        )
        if not found:
            raise DataUnavailable(
                f"no Upstox {option_type or 'CE/PE'} contracts for "
                f"{underlying_symbol!r} expiring {expiry.isoformat()}"
            )
        return tuple(sorted(found, key=lambda i: (i.strike or 0.0, i.option_type or "")))


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitTier:
    name: str
    per_second: int | None
    per_minute: int | None
    per_30_minutes: int | None


RATE_LIMIT_TIERS = {
    "standard": RateLimitTier("standard", 50, 500, 2000),
    "order_placement": RateLimitTier("order_placement", 10, 500, 2000),
    "order_placement_registered_algo": RateLimitTier(
        "order_placement_registered_algo", 50, 500, 2000
    ),
    "totp_login": RateLimitTier("totp_login", 1, 10, 60),
    "apply_ipo": RateLimitTier("apply_ipo", 1, 10, 300),
}


class UpstoxRateLimiter:
    """Sliding-window limiter for the documented per-second/minute/30-minute caps.

    Upstox documents no breach status code, no error code and no retry header,
    so there is nothing to react to after the fact: the only safe approach is to
    not exceed the limit in the first place, and to refuse the call locally when
    it would. `RateLimited` carries the wait needed to make the call legal.
    """

    WINDOWS = ((1.0, "per_second"), (60.0, "per_minute"), (1800.0, "per_30_minutes"))

    def __init__(self, tier: RateLimitTier | str = "standard") -> None:
        self._tier = RATE_LIMIT_TIERS[tier] if isinstance(tier, str) else tier
        self._hits: list[float] = []

    @property
    def tier(self) -> RateLimitTier:
        return self._tier

    def _prune(self, now: float) -> None:
        horizon = now - 1800.0
        self._hits = [hit for hit in self._hits if hit > horizon]

    def retry_after(self, now: float) -> float:
        """Seconds to wait before a call would be within every window, 0 if now."""
        self._prune(now)
        wait = 0.0
        for span, attribute in self.WINDOWS:
            cap = getattr(self._tier, attribute)
            if cap is None:
                continue
            in_window = [hit for hit in self._hits if hit > now - span]
            if len(in_window) >= cap:
                # The oldest call in the window has to age out.
                oldest_relevant = sorted(in_window)[len(in_window) - cap]
                wait = max(wait, oldest_relevant + span - now)
        return max(0.0, wait)

    def acquire(self, now: float) -> None:
        wait = self.retry_after(now)
        if wait > 0:
            raise RateLimited(
                f"Upstox {self._tier.name} rate limit reached "
                f"({self._tier.per_second}/s, {self._tier.per_minute}/min, "
                f"{self._tier.per_30_minutes}/30min); wait {wait:.3f}s",
                retry_after_seconds=wait,
            )
        self._hits.append(now)

    def try_acquire(self, now: float) -> bool:
        try:
            self.acquire(now)
        except RateLimited:
            return False
        return True


# --------------------------------------------------------------------------
# Feed subscription management
# --------------------------------------------------------------------------


class SubscriptionLimitExceeded(MarketDataError):
    """A subscription request would exceed a documented feed limit."""


@dataclass
class UpstoxSubscription:
    """Tracks what is subscribed, in which mode, against the documented caps.

    Limits are enforced locally. The documentation's "individual" and "combined"
    columns are not fully unambiguous, so the stricter reading is applied: the
    combined cap is the smallest combined value among the active modes.
    """

    plus_tier: bool = False
    _modes: dict[FeedMode, set[str]] = field(default_factory=dict)

    @property
    def max_connections(self) -> int:
        return MAX_CONNECTIONS["plus" if self.plus_tier else "normal"]

    def keys(self, mode: FeedMode | None = None) -> frozenset[str]:
        if mode is not None:
            return frozenset(self._modes.get(mode, set()))
        return frozenset().union(*self._modes.values()) if self._modes else frozenset()

    def mode_of(self, instrument_key: str) -> FeedMode | None:
        for mode, keys in self._modes.items():
            if instrument_key in keys:
                return mode
        return None

    @property
    def total(self) -> int:
        return len(self.keys())

    def _combined_cap(self, modes: Iterable[FeedMode]) -> int:
        caps = [FEED_MODE_LIMITS[mode][1] for mode in modes]
        return min(caps) if caps else 0

    def _check(self, mode: FeedMode, additions: set[str]) -> None:
        individual, _, needs_plus = FEED_MODE_LIMITS[mode]
        if needs_plus and not self.plus_tier:
            raise SubscriptionLimitExceeded(
                f"feed mode {mode.value!r} is documented only for the Upstox Plus tier"
            )
        projected = self._modes.get(mode, set()) | additions
        if len(projected) > individual:
            raise SubscriptionLimitExceeded(
                f"{len(projected)} instruments in mode {mode.value!r} exceeds the "
                f"documented individual limit of {individual}"
            )
        active = {existing for existing, keys in self._modes.items() if keys} | {mode}
        if len(active) > 1:
            total = len(self.keys() | additions)
            combined = self._combined_cap(active)
            if total > combined:
                raise SubscriptionLimitExceeded(
                    f"{total} instruments across modes "
                    f"{sorted(m.value for m in active)} exceeds the documented "
                    f"combined limit of {combined}"
                )

    def subscribe(
        self, instrument_keys: Iterable[str], mode: FeedMode = FeedMode.LTPC, guid: str | None = None
    ) -> dict:
        keys = {str(key) for key in instrument_keys}
        if not keys:
            raise MarketDataError("subscribe() needs at least one instrument key")
        # A key already subscribed in another mode would be a silent mode change.
        for key in keys:
            current = self.mode_of(key)
            if current is not None and current is not mode:
                raise MarketDataError(
                    f"{key!r} is already subscribed in mode {current.value!r}; "
                    "use change_mode() rather than re-subscribing"
                )
        self._check(mode, keys)
        self._modes.setdefault(mode, set()).update(keys)
        return self._request("sub", mode, keys, guid)

    def change_mode(
        self, instrument_keys: Iterable[str], mode: FeedMode, guid: str | None = None
    ) -> dict:
        keys = {str(key) for key in instrument_keys}
        unknown = {key for key in keys if self.mode_of(key) is None}
        if unknown:
            raise MarketDataError(
                f"cannot change mode for unsubscribed keys: {sorted(unknown)}"
            )
        for existing in list(self._modes):
            self._modes[existing] -= keys
        self._check(mode, keys)
        self._modes.setdefault(mode, set()).update(keys)
        return self._request("change_mode", mode, keys, guid)

    def unsubscribe(self, instrument_keys: Iterable[str], guid: str | None = None) -> dict:
        keys = {str(key) for key in instrument_keys}
        if not keys:
            raise MarketDataError("unsubscribe() needs at least one instrument key")
        for existing in list(self._modes):
            self._modes[existing] -= keys
        return self._request("unsub", None, keys, guid)

    @staticmethod
    def _request(
        method: str, mode: FeedMode | None, keys: set[str], guid: str | None
    ) -> dict:
        data: dict[str, Any] = {"instrumentKeys": sorted(keys)}
        if mode is not None:
            data["mode"] = mode.value
        return {"guid": guid or uuid.uuid4().hex, "method": method, "data": data}

    @staticmethod
    def encode(request: dict) -> bytes:
        """Upstox requires the request as a binary frame, not a text frame."""
        return json.dumps(request).encode("utf-8")


# --------------------------------------------------------------------------
# Feed message decoding and normalization
# --------------------------------------------------------------------------


class FeedDecoder(Protocol):
    """Turns a raw feed frame into the documented decoded structure."""

    def decode(self, frame: bytes) -> dict: ...


class ProtobufFeedDecoder:
    """Protobuf decoding, which needs generated stubs this repo does not carry.

    Generating them requires downloading the `.proto` and running `protoc`, or
    installing Upstox's own SDK. Rather than vendor a guess at the schema, this
    raises with the exact URL and the import it looked for. Everything
    downstream of the decode is implemented and tested against the documented
    decoded shape, so swapping a working decoder in is a one-line change.
    """

    def __init__(self, module_name: str = "MarketDataFeedV3_pb2") -> None:
        self._module_name = module_name

    def decode(self, frame: bytes) -> dict:
        raise CredentialsMissing(
            "Upstox feed frames are Protobuf-encoded and no generated schema is "
            f"available (looked for {self._module_name!r}). Fetch "
            f"{FEED_PROTO_URL} and compile it with protoc, or install Upstox's "
            "SDK, then pass a decoder that returns the documented decoded dict."
        )


class JsonFeedDecoder:
    """Decoder for recorded fixtures stored as JSON in the decoded shape.

    This is what makes the feed path replayable offline: a captured session can
    be stored as JSON lines and pushed through exactly the normalization the
    live path uses.
    """

    def decode(self, frame: bytes) -> dict:
        try:
            payload = json.loads(frame)
        except json.JSONDecodeError as exc:
            raise MarketDataError(f"feed fixture frame is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise MarketDataError(
                f"feed frame should decode to an object, got {type(payload).__name__}"
            )
        return payload


@dataclass(frozen=True)
class FeedUpdate:
    """One instrument's update, normalized out of a `live_feed` message."""

    instrument_key: str
    tick: Tick | None
    quote: Quote | None
    feed_mode: str
    message_time: datetime | None
    ohlc: tuple[dict, ...] = ()


class UpstoxFeedNormalizer:
    """Turns decoded feed messages into engine ticks and quotes.

    Written against the documented decoded shape:
    `{"type": "live_feed", "currentTs": "<ms>", "feeds": {"<key>": {...}}}` where
    each feed carries `ltpc`, or `fullFeed.marketFF` (`ltpc`, `marketLevel.bidAskQuote`,
    `marketOHLC`), or `firstLevelWithGreeks` (`ltpc`, `firstDepth`).

    Two defences that matter more than the parsing:

    * **Quote timestamp mismatch.** A per-instrument timestamp far from the
      message's own `currentTs` means the feed is replaying something stale into
      a live stream. It is reported, not averaged away.
    * **Malformed data raises.** A missing or non-positive last price, or a
      crossed book, is an error. Returning a zero price here would reach the
      paper broker as a fill.
    """

    def __init__(
        self,
        *,
        key_to_symbol: Callable[[str], str] | None = None,
        max_quote_timestamp_skew: timedelta = timedelta(seconds=30),
    ) -> None:
        self._key_to_symbol = key_to_symbol or (lambda key: f"{PROVIDER_NAME}:{key}")
        self._max_skew = max_quote_timestamp_skew

    def normalize_message(self, message: dict) -> tuple[FeedUpdate, ...]:
        message_type = message.get("type")
        if message_type == "market_info":
            return ()
        feeds = message.get("feeds")
        if not isinstance(feeds, dict):
            raise MarketDataError(
                f"feed message has no 'feeds' object (type={message_type!r})"
            )
        current_ts = (
            epoch_millis_to_utc(message["currentTs"]) if message.get("currentTs") else None
        )
        return tuple(
            self.normalize_feed(key, payload, current_ts)
            for key, payload in feeds.items()
        )

    def normalize_feed(
        self, instrument_key: str, payload: dict, message_time: datetime | None
    ) -> FeedUpdate:
        provenance = Provenance(
            provider=PROVIDER_NAME,
            fetched_at=message_time or datetime.now(tz=timezone.utc),
            endpoint=FEED_PATH,
            is_mock=False,
        )
        symbol = self._key_to_symbol(instrument_key)

        ltpc, depth, ohlc, mode = self._extract(payload)
        tick = self._to_tick(symbol, ltpc, provenance, message_time) if ltpc else None
        quote = (
            self._to_quote(symbol, depth, ltpc, provenance, message_time) if depth else None
        )
        return FeedUpdate(
            instrument_key=instrument_key,
            tick=tick,
            quote=quote,
            feed_mode=mode,
            message_time=message_time,
            ohlc=tuple(ohlc),
        )

    @staticmethod
    def _extract(payload: dict) -> tuple[dict | None, list[dict], list[dict], str]:
        if "fullFeed" in payload:
            full = payload["fullFeed"]
            body = full.get("marketFF") or full.get("indexFF") or {}
            depth = (
                body.get("marketLevel", {}).get("bidAskQuote", [])
                if isinstance(body.get("marketLevel"), dict)
                else []
            )
            ohlc = (
                body.get("marketOHLC", {}).get("ohlc", [])
                if isinstance(body.get("marketOHLC"), dict)
                else []
            )
            return body.get("ltpc"), list(depth), list(ohlc), "full"
        if "firstLevelWithGreeks" in payload:
            body = payload["firstLevelWithGreeks"]
            first = body.get("firstDepth")
            return body.get("ltpc"), [first] if first else [], [], "option_greeks"
        if "ltpc" in payload:
            return payload["ltpc"], [], [], "ltpc"
        raise MarketDataError(
            f"unrecognised feed payload shape: keys={sorted(payload)[:8]}"
        )

    def _to_tick(
        self,
        symbol: str,
        ltpc: dict,
        provenance: Provenance,
        message_time: datetime | None,
    ) -> Tick:
        if "ltp" not in ltpc:
            raise MarketDataError(f"{symbol}: feed ltpc has no 'ltp' ({sorted(ltpc)})")
        try:
            last_price = float(ltpc["ltp"])
        except (TypeError, ValueError):
            raise MarketDataError(f"{symbol}: non-numeric ltp {ltpc['ltp']!r}") from None
        if not last_price > 0:
            raise MarketDataError(f"{symbol}: non-positive ltp {last_price!r}")

        exchange_time = (
            epoch_millis_to_utc(ltpc["ltt"])
            if ltpc.get("ltt")
            else message_time or datetime.now(tz=timezone.utc)
        )
        self._check_skew(symbol, exchange_time, message_time, "ltt")
        quantity = ltpc.get("ltq")
        return Tick(
            symbol=symbol,
            exchange_time=exchange_time,
            last_price=last_price,
            last_quantity=int(quantity) if quantity not in (None, "") else None,
            provenance=provenance,
        )

    def _to_quote(
        self,
        symbol: str,
        depth: list[dict],
        ltpc: dict | None,
        provenance: Provenance,
        message_time: datetime | None,
    ) -> Quote:
        best = depth[0] or {}
        bid = best.get("bidP")
        ask = best.get("askP")
        quoted_at = (
            epoch_millis_to_utc(ltpc["ltt"])
            if ltpc and ltpc.get("ltt")
            else message_time or datetime.now(tz=timezone.utc)
        )
        self._check_skew(symbol, quoted_at, message_time, "quote")

        def positive(value: Any) -> float | None:
            if value in (None, ""):
                return None
            number = float(value)
            # Upstox publishes 0 for an absent side; a zero-priced bid is not a
            # bid, and treating it as one would invent a tradable book.
            return number if number > 0 else None

        bid_price, ask_price = positive(bid), positive(ask)
        if bid_price is not None and ask_price is not None and ask_price < bid_price:
            raise MarketDataError(
                f"{symbol}: crossed book from the feed, bid {bid_price} > ask {ask_price}"
            )
        return Quote(
            symbol=symbol,
            quoted_at=quoted_at,
            bid=bid_price,
            ask=ask_price,
            bid_quantity=int(best["bidQ"]) if best.get("bidQ") not in (None, "") else None,
            ask_quantity=int(best["askQ"]) if best.get("askQ") not in (None, "") else None,
            last_price=float(ltpc["ltp"]) if ltpc and ltpc.get("ltp") else None,
            provenance=provenance,
        )

    def _check_skew(
        self,
        symbol: str,
        observed: datetime,
        message_time: datetime | None,
        what: str,
    ) -> None:
        if message_time is None:
            return
        drift = abs(observed - message_time)
        if drift > self._max_skew:
            raise MarketDataError(
                f"{symbol}: feed {what} timestamp {observed.isoformat()} differs from "
                f"the message's currentTs {message_time.isoformat()} by "
                f"{drift.total_seconds():.0f}s, beyond the "
                f"{self._max_skew.total_seconds():.0f}s tolerance"
            )


# --------------------------------------------------------------------------
# Historical candles
# --------------------------------------------------------------------------


def historical_candle_path(
    instrument_key: str, unit: str, interval: int, to_date: date, from_date: date
) -> str:
    """Build the v3 historical-candle path.

    `to_date` precedes `from_date` in the URL, which is easy to get backwards;
    the argument order here is chronological and the swap happens once, in one
    place. The pipe in an instrument key is URL-encoded.
    """
    if from_date > to_date:
        raise ValueError(f"from_date {from_date} is after to_date {to_date}")
    allowed = {"minutes": range(1, 301), "hours": range(1, 6), "days": [1], "weeks": [1], "months": [1]}
    if unit not in allowed:
        raise ValueError(f"unsupported Upstox unit {unit!r}; expected one of {sorted(allowed)}")
    if interval not in allowed[unit]:
        raise ValueError(f"interval {interval} is not documented for unit {unit!r}")
    return HISTORICAL_CANDLE_PATH.format(
        instrument_key=instrument_key.replace("|", "%7C"),
        unit=unit,
        interval=interval,
        to_date=to_date.isoformat(),
        from_date=from_date.isoformat(),
    )


def max_request_days(unit: str, interval: int) -> int:
    if unit == "minutes":
        return MAX_REQUEST_DAYS["minutes_upto_15" if interval <= 15 else "minutes_above_15"]
    if unit == "hours":
        return MAX_REQUEST_DAYS["hours"]
    return MAX_REQUEST_DAYS["days"]


def earliest_available(unit: str) -> date:
    return EARLIEST_INTRADAY if unit in ("minutes", "hours") else EARLIEST_DAILY
