"""Half Trend Regression alert -> normalized internal signal event.

This adapter applies *contract* policy only: is this a well-formed, fresh,
non-repudiated HTR reversal alert on an allowed symbol and timeframe? It makes
no judgement about whether the signal is worth trading -- that is the behavioral
decision engine's job (P3/P4), and it runs on a separate code path.

Checks are ordered cheapest-and-most-specific first so that the reason code a
caller receives names the first genuine contract violation rather than a
downstream symptom.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.signals.schemas import (
    NormalizedSignalEvent,
    RawTradingViewAlert,
    ReasonCode,
    SignalEventKind,
    parse_direction,
    to_utc,
)
from app.signals.timeframes import (
    UnsupportedTimeframe,
    bar_close_from_open,
    canonical_timeframe,
    timeframe_duration,
)

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class SignalPolicy:
    expected_indicator: str
    allowed_symbols: tuple[str, ...]
    allowed_timeframes: tuple[str, ...]
    max_signal_age_seconds: int = 120
    clock_skew_tolerance_seconds: int = 10
    required_mode: str = "PAPER"
    # IST session-open time used as the intraday bar grid anchor. None disables
    # the alignment check rather than guessing an anchor.
    session_open_ist: time | None = None


@dataclass(frozen=True)
class NormalizationResult:
    event: NormalizedSignalEvent | None
    reason: ReasonCode | None = None
    detail: str | None = None

    @property
    def accepted(self) -> bool:
        return self.event is not None


def _reject(reason: ReasonCode, detail: str) -> NormalizationResult:
    return NormalizationResult(event=None, reason=reason, detail=detail)


def normalize_alert(
    payload: object, now: datetime, policy: SignalPolicy
) -> NormalizationResult:
    """Validate and normalize one inbound alert body."""
    now = to_utc(now)
    skew = timedelta(seconds=policy.clock_skew_tolerance_seconds)

    try:
        alert = RawTradingViewAlert.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "<root>"
        return _reject(ReasonCode.SCHEMA_INVALID, f"{location}: {first['msg']}")

    if alert.indicator.strip() != policy.expected_indicator.strip():
        return _reject(ReasonCode.UNEXPECTED_INDICATOR, alert.indicator)

    # A trend that is merely still red is not a new reversal. Only the
    # indicator's direction-change event is a candidate signal.
    if alert.event.strip().upper() != SignalEventKind.REVERSAL.value:
        return _reject(ReasonCode.NOT_A_REVERSAL_EVENT, alert.event)

    direction = parse_direction(alert.signal)
    if direction is None:
        return _reject(ReasonCode.UNKNOWN_SIGNAL_DIRECTION, alert.signal)

    if alert.symbol not in policy.allowed_symbols:
        return _reject(ReasonCode.UNSUPPORTED_SYMBOL, alert.symbol)

    try:
        timeframe = canonical_timeframe(alert.interval)
    except UnsupportedTimeframe as exc:
        return _reject(ReasonCode.UNSUPPORTED_TIMEFRAME, str(exc))
    if timeframe not in policy.allowed_timeframes:
        return _reject(ReasonCode.UNSUPPORTED_TIMEFRAME, timeframe)

    if alert.mode.strip().upper() != policy.required_mode:
        return _reject(ReasonCode.MODE_NOT_PAPER, alert.mode)

    try:
        price = float(alert.signal_price)
    except (TypeError, ValueError):
        return _reject(ReasonCode.INVALID_SIGNAL_PRICE, str(alert.signal_price))
    if not price > 0 or price != price or price in (float("inf"), float("-inf")):
        return _reject(ReasonCode.INVALID_SIGNAL_PRICE, str(alert.signal_price))

    # `{{time}}` is the bar's OPEN time; the close time is derived, never assumed.
    try:
        bar_close_time = bar_close_from_open(alert.bar_open_time, timeframe)
    except UnsupportedTimeframe as exc:
        return _reject(ReasonCode.UNSUPPORTED_TIMEFRAME, str(exc))

    if policy.session_open_ist is not None:
        offset = _offset_from_session_open(alert.bar_open_time, policy.session_open_ist)
        bar_length = timeframe_duration(timeframe)
        if offset < timedelta(0) or offset % bar_length:
            minutes = offset.total_seconds() / 60
            local = alert.bar_open_time.astimezone(IST)
            return _reject(
                ReasonCode.BAR_NOT_ALIGNED_TO_TIMEFRAME,
                f"bar_open {alert.bar_open_time.isoformat()} ({local:%H:%M} IST) is "
                f"{minutes:+.0f} min from that day's "
                f"{policy.session_open_ist:%H:%M} IST session open, "
                f"not a whole number of {timeframe} bars",
            )

    if alert.fired_at > now + skew:
        return _reject(
            ReasonCode.FUTURE_TIMESTAMP, f"fired_at={alert.fired_at.isoformat()} > now"
        )
    if alert.bar_open_time > now + skew:
        return _reject(
            ReasonCode.FUTURE_TIMESTAMP,
            f"bar_open_time={alert.bar_open_time.isoformat()} > now",
        )
    if now - alert.fired_at > timedelta(seconds=policy.max_signal_age_seconds):
        age = (now - alert.fired_at).total_seconds()
        return _reject(ReasonCode.STALE_SIGNAL, f"delivery age {age:.0f}s")

    # A `Once Per Bar Close` alert cannot legitimately fire before its bar has
    # closed. If it did, the payload is describing an unconfirmed (repainting)
    # bar and must not be treated as a confirmed signal.
    if bar_close_time > alert.fired_at + skew:
        return _reject(
            ReasonCode.BAR_CLOSE_AFTER_FIRE_TIME,
            f"bar_close={bar_close_time.isoformat()} fired_at={alert.fired_at.isoformat()}",
        )

    event = NormalizedSignalEvent(
        indicator_version=alert.indicator_version,
        symbol=alert.symbol,
        timeframe=timeframe,
        signal=direction,
        bar_open_time=alert.bar_open_time,
        bar_close_time=bar_close_time,
        fired_at=alert.fired_at,
        received_at=now,
        signal_price=price,
        mode=policy.required_mode,
        idempotency_key=NormalizedSignalEvent.build_idempotency_key(
            symbol=alert.symbol,
            timeframe=timeframe,
            signal=direction,
            bar_close_time=bar_close_time,
        ),
    )
    return NormalizationResult(event=event)


def _offset_from_session_open(bar_open: datetime, session_open: time) -> timedelta:
    """Distance from that trading day's session open to this bar's open.

    TradingView anchors intraday bars to the session open, so a genuine
    `Once Per Bar Close` alert on an N-minute chart always carries a bar_open
    that is a whole number of N-minute bars after the open. Anchoring on UTC
    midnight instead would wrongly reject timeframes that do not divide the
    09:15 IST offset evenly.
    """
    local = bar_open.astimezone(IST)
    anchor = local.replace(
        hour=session_open.hour, minute=session_open.minute, second=0, microsecond=0
    )
    return local - anchor


def policy_from_settings(settings, session_open_ist: time | None = None) -> SignalPolicy:
    """Build the contract policy from typed application settings."""
    return SignalPolicy(
        expected_indicator=settings.expected_indicator,
        allowed_symbols=settings.symbol_allowlist,
        allowed_timeframes=settings.timeframe_allowlist,
        max_signal_age_seconds=settings.max_signal_age_seconds,
        clock_skew_tolerance_seconds=settings.clock_skew_tolerance_seconds,
        required_mode=settings.trading_mode,
        session_open_ist=session_open_ist,
    )
