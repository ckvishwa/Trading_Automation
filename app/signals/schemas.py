"""Signal contracts: the raw TradingView alert and the normalized internal event.

Two models, deliberately separate:

* `RawTradingViewAlert` is permissive about field naming so that alerts already
  configured on the user's chart (which used `direction`/`bar_time`/`index_close`)
  keep working alongside the current field names. It validates shape, not policy.
* `NormalizedSignalEvent` is the single internal representation every downstream
  stage consumes. It is timezone-aware UTC throughout and carries the
  idempotency key used for exactly-once storage.

A BUY/SELL signal is a *candidate direction* from Half Trend Regression. It is
not a broker instruction and not a decision to trade.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

SCHEMA_VERSION = "1.0"
INDICATOR_KEY = "half_trend_regression"

ShortStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class SignalDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalEventKind(StrEnum):
    REVERSAL = "REVERSAL"


class ReasonCode(StrEnum):
    """Every rejection in the system names one of these. No silent failures."""

    # --- Ingress / contract (P1) -----------------------------------------
    UNAUTHORIZED = "UNAUTHORIZED"
    MALFORMED_JSON = "MALFORMED_JSON"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNEXPECTED_INDICATOR = "UNEXPECTED_INDICATOR"
    NOT_A_REVERSAL_EVENT = "NOT_A_REVERSAL_EVENT"
    UNKNOWN_SIGNAL_DIRECTION = "UNKNOWN_SIGNAL_DIRECTION"
    UNSUPPORTED_SYMBOL = "UNSUPPORTED_SYMBOL"
    UNSUPPORTED_TIMEFRAME = "UNSUPPORTED_TIMEFRAME"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    STALE_SIGNAL = "STALE_SIGNAL"
    BAR_CLOSE_AFTER_FIRE_TIME = "BAR_CLOSE_AFTER_FIRE_TIME"
    BAR_NOT_ALIGNED_TO_TIMEFRAME = "BAR_NOT_ALIGNED_TO_TIMEFRAME"
    INVALID_SIGNAL_PRICE = "INVALID_SIGNAL_PRICE"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    MODE_NOT_PAPER = "MODE_NOT_PAPER"

    # --- Decision engine (P4; defined now, enforced in later milestones) --
    INSUFFICIENT_HISTORICAL_EVIDENCE = "INSUFFICIENT_HISTORICAL_EVIDENCE"
    NEGATIVE_OUT_OF_SAMPLE_EXPECTANCY = "NEGATIVE_OUT_OF_SAMPLE_EXPECTANCY"
    UNVERIFIED_SIGNAL = "UNVERIFIED_SIGNAL"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    PATTERN_MISMATCH = "PATTERN_MISMATCH"
    UNSUITABLE_OPTION_LIQUIDITY = "UNSUITABLE_OPTION_LIQUIDITY"
    EXCESSIVE_RISK = "EXCESSIVE_RISK"
    OUTSIDE_TRADING_SESSION = "OUTSIDE_TRADING_SESSION"
    DATA_INTEGRITY_FAILURE = "DATA_INTEGRITY_FAILURE"


_DIRECTION_SYNONYMS: dict[str, SignalDirection] = {
    "BUY": SignalDirection.BUY,
    "BULLISH": SignalDirection.BUY,
    "GREEN": SignalDirection.BUY,
    "LONG": SignalDirection.BUY,
    "UP": SignalDirection.BUY,
    "SELL": SignalDirection.SELL,
    "BEARISH": SignalDirection.SELL,
    "RED": SignalDirection.SELL,
    "SHORT": SignalDirection.SELL,
    "DOWN": SignalDirection.SELL,
}


def parse_direction(value: Any) -> SignalDirection | None:
    """Map an alert's direction wording onto BUY/SELL, or None if unrecognised."""
    return _DIRECTION_SYNONYMS.get(str(value).strip().upper())


def to_utc(value: datetime) -> datetime:
    """Normalize an aware or naive datetime to UTC.

    A naive timestamp is interpreted as UTC: TradingView emits `{{time}}` and
    `{{timenow}}` in UTC, so this is the only correct reading of a bare value.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RawTradingViewAlert(BaseModel):
    """Shape of the JSON body TradingView posts, as authored in the alert message."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    indicator: ShortStr
    event: ShortStr = Field(default="REVERSAL")
    signal: ShortStr = Field(validation_alias=AliasChoices("signal", "direction"))
    exchange: ShortStr
    ticker: ShortStr
    interval: ShortStr
    signal_price: float | str = Field(
        validation_alias=AliasChoices("signal_price", "index_close", "close", "price")
    )
    bar_open_time: datetime = Field(
        validation_alias=AliasChoices("bar_open_time", "bar_time", "time")
    )
    fired_at: datetime = Field(validation_alias=AliasChoices("fired_at", "timenow"))
    indicator_version: str | None = Field(default=None, max_length=120)
    mode: str = Field(default="PAPER", max_length=16)

    @field_validator("bar_open_time", "fired_at")
    @classmethod
    def _as_utc(cls, value: datetime) -> datetime:
        return to_utc(value)

    @property
    def symbol(self) -> str:
        return f"{self.exchange.strip().upper()}:{self.ticker.strip().upper()}"


class NormalizedSignalEvent(BaseModel):
    """The internal signal contract. Every downstream stage reads only this."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    source: str = "tradingview"
    indicator: str = INDICATOR_KEY
    indicator_version: str | None = None
    symbol: str
    timeframe: str
    signal: SignalDirection
    event: SignalEventKind = SignalEventKind.REVERSAL
    bar_open_time: datetime
    bar_close_time: datetime
    fired_at: datetime
    received_at: datetime
    signal_price: float = Field(gt=0)
    mode: str = "PAPER"
    idempotency_key: str

    @staticmethod
    def build_idempotency_key(
        *, symbol: str, timeframe: str, signal: SignalDirection, bar_close_time: datetime
    ) -> str:
        """Stable key for one indicator event on one bar.

        Derived from signal identity rather than delivery metadata, so TradingView
        re-deliveries, process restarts and manual replays all collapse onto the
        same key. `fired_at` is excluded on purpose: a retried webhook carries a
        new `{{timenow}}` for the same bar event.
        """
        material = "|".join(
            [
                INDICATOR_KEY,
                symbol.upper(),
                timeframe.lower(),
                signal.value,
                to_utc(bar_close_time).isoformat(timespec="seconds"),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
