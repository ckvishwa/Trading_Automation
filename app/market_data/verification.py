"""Explicit market-feed verification states and candle parity.

A connected socket is not a verified feed, and that distinction is the whole
point of this module. The states are a lattice, each stricter than the last:

    AUTHENTICATION_BLOCKED   no usable credential -> nothing was attempted
    AUTHENTICATION_FAILED    a credential existed but the provider rejected it
    SOCKET_CONNECTED         the socket opened, but no valid tick has arrived
    MARKET_CLOSED            the session calendar says the market was not open
    LIVE_DATA_INVALID        ticks arrived but failed validation
    LIVE_STREAM_VERIFIED     valid ticks and at least one completed candle
    HISTORICAL_PARITY_VERIFIED  built candles matched the official historical record

`LIVE_STREAM_VERIFIED` is only reached with completed candles built from
validated ticks; `HISTORICAL_PARITY_VERIFIED` only when those candles match
Upstox's own historical candles bar for bar. Tick completeness cannot be
established from a stream alone -- a dropped update leaves no trace -- so
historical candles are the canonical source, and a parity mismatch is reported
rather than averaged away.

Nothing here fabricates a candle. A bar present in history but missing from the
stream is reported as missing; it is never filled from the surrounding bars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum

from app.market_data.contracts import Candle


class VerificationState(StrEnum):
    AUTHENTICATION_BLOCKED = "AUTHENTICATION_BLOCKED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    SOCKET_CONNECTED = "SOCKET_CONNECTED"
    MARKET_CLOSED = "MARKET_CLOSED"
    LIVE_DATA_INVALID = "LIVE_DATA_INVALID"
    LIVE_STREAM_VERIFIED = "LIVE_STREAM_VERIFIED"
    HISTORICAL_PARITY_VERIFIED = "HISTORICAL_PARITY_VERIFIED"


class _Rank(IntEnum):
    # Ordering for "the best state reached", so a run that connected but saw no
    # tick cannot be reported as merely blocked.
    AUTHENTICATION_BLOCKED = 0
    AUTHENTICATION_FAILED = 1
    MARKET_CLOSED = 2
    SOCKET_CONNECTED = 3
    LIVE_DATA_INVALID = 4
    LIVE_STREAM_VERIFIED = 5
    HISTORICAL_PARITY_VERIFIED = 6


# States from which a live-data claim may be made. Everything else is a
# not-yet-verified outcome.
VERIFIED_STATES = frozenset(
    {VerificationState.LIVE_STREAM_VERIFIED, VerificationState.HISTORICAL_PARITY_VERIFIED}
)


def rank(state: VerificationState) -> int:
    return int(_Rank[state.name])


@dataclass(frozen=True)
class OHLCMismatch:
    open_time: datetime
    field: str
    built: float
    historical: float

    @property
    def absolute(self) -> float:
        return abs(self.built - self.historical)


@dataclass(frozen=True)
class ParityReport:
    compared: int = 0
    matched: int = 0
    mismatches: tuple[OHLCMismatch, ...] = ()
    missing_in_built: tuple[datetime, ...] = ()
    extra_in_built: tuple[datetime, ...] = ()

    @property
    def parity(self) -> bool:
        # Exact agreement over a non-empty overlap. An empty comparison is never
        # parity: nothing was actually checked.
        return (
            self.compared > 0
            and not self.mismatches
            and not self.missing_in_built
            and not self.extra_in_built
        )

    def as_dict(self) -> dict:
        return {
            "compared": self.compared,
            "matched": self.matched,
            "mismatches": [
                {
                    "open_time": mismatch.open_time.isoformat(),
                    "field": mismatch.field,
                    "built": mismatch.built,
                    "historical": mismatch.historical,
                }
                for mismatch in self.mismatches
            ],
            "missing_in_built": [moment.isoformat() for moment in self.missing_in_built],
            "extra_in_built": [moment.isoformat() for moment in self.extra_in_built],
            "parity": self.parity,
        }


def compare_candles(
    built: list[Candle] | tuple[Candle, ...],
    historical: list[Candle] | tuple[Candle, ...],
    *,
    price_tolerance: float = 0.0,
) -> ParityReport:
    """Compare locally built candles against the canonical historical record.

    Matched by `open_time`. OHLC is compared exactly by default; a tolerance is
    offered because a provider may round differently, but it defaults to zero so
    a real discrepancy is not waved through. A bar in history but not in the
    built set is `missing_in_built`; the reverse is `extra_in_built`. Neither is
    ever interpolated away.
    """
    built_by_time = {candle.open_time: candle for candle in built}
    historical_by_time = {candle.open_time: candle for candle in historical}
    overlap = sorted(set(built_by_time) & set(historical_by_time))

    mismatches: list[OHLCMismatch] = []
    matched = 0
    for moment in overlap:
        b, h = built_by_time[moment], historical_by_time[moment]
        bar_ok = True
        for name in ("open", "high", "low", "close"):
            bv, hv = getattr(b, name), getattr(h, name)
            if abs(bv - hv) > price_tolerance:
                mismatches.append(OHLCMismatch(moment, name, bv, hv))
                bar_ok = False
        if bar_ok:
            matched += 1

    missing = tuple(sorted(set(historical_by_time) - set(built_by_time)))
    extra = tuple(sorted(set(built_by_time) - set(historical_by_time)))
    return ParityReport(
        compared=len(overlap),
        matched=matched,
        mismatches=tuple(mismatches),
        missing_in_built=missing,
        extra_in_built=extra,
    )


@dataclass
class FeedVerification:
    """Accumulates evidence and resolves it to a single verification state.

    It is fed facts -- a credential was present, the socket opened, N valid
    ticks arrived, a candle completed, parity held -- and reports the strongest
    state those facts support. It cannot be told it is verified; it can only be
    shown ticks and candles.
    """

    has_credential: bool = False
    authenticated: bool | None = None
    socket_connected: bool = False
    market_open: bool | None = None
    valid_ticks: int = 0
    invalid_ticks: int = 0
    completed_candles: int = 0
    parity: ParityReport | None = None
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def state(self) -> VerificationState:
        if not self.has_credential:
            return VerificationState.AUTHENTICATION_BLOCKED
        if self.authenticated is False:
            return VerificationState.AUTHENTICATION_FAILED
        if not self.socket_connected:
            # A credential existed and auth did not fail, but no socket opened;
            # that is an attempt that did not get off the ground.
            return (
                VerificationState.AUTHENTICATION_FAILED
                if self.authenticated is None
                else VerificationState.SOCKET_CONNECTED
            )
        if self.parity is not None and self.parity.parity:
            return VerificationState.HISTORICAL_PARITY_VERIFIED
        if self.completed_candles > 0 and self.valid_ticks > 0:
            return VerificationState.LIVE_STREAM_VERIFIED
        if self.invalid_ticks > 0 and self.valid_ticks == 0:
            return VerificationState.LIVE_DATA_INVALID
        if self.market_open is False and self.valid_ticks == 0:
            # Connected, no ticks, and the calendar says the market was shut:
            # not a failure, just nothing to verify.
            return VerificationState.MARKET_CLOSED
        return VerificationState.SOCKET_CONNECTED

    @property
    def verified(self) -> bool:
        return self.state() in VERIFIED_STATES

    def as_dict(self) -> dict:
        return {
            "state": self.state().value,
            "verified_against_live_account": self.verified,
            "has_credential": self.has_credential,
            "authenticated": self.authenticated,
            "socket_connected": self.socket_connected,
            "market_open": self.market_open,
            "valid_ticks": self.valid_ticks,
            "invalid_ticks": self.invalid_ticks,
            "completed_candles": self.completed_candles,
            "parity": self.parity.as_dict() if self.parity is not None else None,
            "notes": list(self.notes),
        }
