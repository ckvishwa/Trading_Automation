"""NSE/BSE session classification.

Rules this module exists to enforce:

1. Session logic is evaluated in `Asia/Kolkata`; every timestamp stored or
   returned is UTC.
2. Exchange schedules are data, never code. They come from a calendar file whose
   shape is documented in `data/reference/nse_calendar.json`.
3. **Unofficial data may only close the market, never open it.** `OPEN` and
   `PRE_OPEN` require both an official session window and a date covered by an
   official holiday list for that segment. A holiday sourced from a broker's
   website can still close a day, because being wrongly closed costs a missed
   trade while being wrongly open costs a trade placed into a market that was
   not there.
4. **Holiday provenance and timing provenance are separate facts, per segment.**
   NSE publishes the derivatives holiday list in its own circular, which states
   no session timings at all. So an official 2026 F&O holiday list does not make
   the F&O session *timings* official, and does not say anything about the cash
   segment. Each segment carries its own `holidays_official_through`.
4. Timings are **effective-dated**: a segment holds a list of session windows,
   each valid from a date, so a timing change does not rewrite history.
5. A special session whose timings are unknown or disputed yields
   `UNSUPPORTED_SESSION`. Nothing here falls back to regular hours. Special
   sessions are checked **before** the weekend rule, because that is exactly
   what they are for: NSE's 2026 Muhurat session falls on a Sunday.

Cash and derivatives are separate segments because their timings and holiday
lists are not guaranteed to match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SUPPORTED_SCHEMA_VERSION = 3


class Segment(StrEnum):
    CASH = "CASH"
    FO = "FO"


class SourceTrust(StrEnum):
    OFFICIAL = "official"
    UNOFFICIAL = "unofficial"
    DISPUTED = "disputed"


class SessionState(StrEnum):
    OPEN = "OPEN"
    PRE_OPEN = "PRE_OPEN"
    CLOSED = "CLOSED"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    UNVERIFIED_CALENDAR = "UNVERIFIED_CALENDAR"
    UNSUPPORTED_SESSION = "UNSUPPORTED_SESSION"


# States in which a trading decision may be made. Everything else fails closed.
TRADEABLE_STATES = frozenset({SessionState.OPEN})


class CalendarError(ValueError):
    """The calendar file cannot be trusted to answer the question asked of it."""


@dataclass(frozen=True)
class SessionWindow:
    effective_from: date
    regular_open: time
    regular_close: time
    source: SourceTrust
    pre_open_start: time | None = None
    pre_open_end: time | None = None
    note: str | None = None

    @property
    def is_official(self) -> bool:
        return self.source is SourceTrust.OFFICIAL


@dataclass(frozen=True)
class SessionStatus:
    state: SessionState
    segment: Segment
    trade_date_ist: date
    local_time_ist: time
    calendar_verified: bool
    detail: str | None = None

    @property
    def is_open(self) -> bool:
        """True only when an official calendar confirms a live regular session."""
        return self.state in TRADEABLE_STATES

    @property
    def may_trade(self) -> bool:
        return self.is_open and self.calendar_verified


def _parse_hhmm(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour=hour, minute=minute)


def _optional_hhmm(value: str | None) -> time | None:
    return None if value in (None, "") else _parse_hhmm(value)


def _parse_window(payload: dict) -> SessionWindow:
    return SessionWindow(
        effective_from=date.fromisoformat(payload["effective_from"]),
        regular_open=_parse_hhmm(payload["regular_open"]),
        regular_close=_parse_hhmm(payload["regular_close"]),
        source=SourceTrust(payload.get("source", SourceTrust.UNOFFICIAL.value)),
        pre_open_start=_optional_hhmm(payload.get("pre_open_start")),
        pre_open_end=_optional_hhmm(payload.get("pre_open_end")),
        note=payload.get("note"),
    )


@dataclass(frozen=True)
class CalendarEntry:
    """A holiday or a special session, with the trust level of its source."""

    date: date
    description: str
    segments: frozenset[str]
    source: SourceTrust
    regular_open: time | None = None
    regular_close: time | None = None

    def applies_to(self, segment: Segment) -> bool:
        return segment.value in self.segments


def _parse_entry(payload: dict) -> CalendarEntry:
    return CalendarEntry(
        date=date.fromisoformat(payload["date"]),
        description=payload.get("description", ""),
        segments=frozenset(payload.get("segments", [s.value for s in Segment])),
        source=SourceTrust(payload.get("source", SourceTrust.UNOFFICIAL.value)),
        regular_open=_optional_hhmm(payload.get("regular_open")),
        regular_close=_optional_hhmm(payload.get("regular_close")),
    )


class SessionCalendar:
    def __init__(self, payload: dict) -> None:
        version = payload.get("schema_version")
        if version != SUPPORTED_SCHEMA_VERSION:
            raise CalendarError(
                f"calendar schema_version {version!r} is not supported; "
                f"this build reads version {SUPPORTED_SCHEMA_VERSION}"
            )

        verified = payload.get("official_verified_through")
        self._verified_through = date.fromisoformat(verified) if verified else None
        self._holiday_coverage = payload.get("holiday_coverage", "unknown")
        self._sources = payload.get("sources", {})

        # Per-segment holiday coverage. NSE's F&O circular can be official for
        # 2026 while the cash list has never been read, so one global horizon
        # would either overstate cash or understate derivatives.
        self._segment_horizons: dict[str, date | None] = {}
        self._segment_notes: dict[str, str | None] = {}

        self._windows: dict[str, tuple[SessionWindow, ...]] = {}
        for segment_name, segment_payload in payload["segments"].items():
            horizon = segment_payload.get("holidays_official_through")
            self._segment_horizons[segment_name] = (
                date.fromisoformat(horizon) if horizon else None
            )
            self._segment_notes[segment_name] = segment_payload.get("holidays_source_note")
            windows = sorted(
                (_parse_window(item) for item in segment_payload["sessions"]),
                key=lambda window: window.effective_from,
            )
            if not windows:
                raise CalendarError(f"segment {segment_name!r} has no session windows")
            self._windows[segment_name] = tuple(windows)

        self._holidays = tuple(_parse_entry(item) for item in payload.get("holidays", ()))
        self._special = tuple(
            _parse_entry(item) for item in payload.get("special_sessions", ())
        )

    @classmethod
    def load(cls, path: Path) -> "SessionCalendar":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # --- introspection ----------------------------------------------------

    @property
    def verified_through(self) -> date | None:
        return self._verified_through

    @property
    def holiday_coverage(self) -> str:
        return self._holiday_coverage

    @property
    def sources(self) -> dict:
        """Provenance of the data in this calendar, including circular refs."""
        return self._sources

    def holidays_official_through(self, segment: Segment = Segment.FO) -> date | None:
        return self._segment_horizons.get(segment.value, self._verified_through)

    def holiday_source_note(self, segment: Segment = Segment.FO) -> str | None:
        return self._segment_notes.get(segment.value)

    def covers(self, trade_date: date, segment: Segment = Segment.FO) -> bool:
        """Whether an official holiday list covers this date for this segment."""
        horizon = self.holidays_official_through(segment)
        return horizon is not None and trade_date <= horizon

    def windows(self, segment: Segment = Segment.FO) -> tuple[SessionWindow, ...]:
        return self._windows[segment.value]

    def window_for(self, trade_date: date, segment: Segment = Segment.FO) -> SessionWindow | None:
        """The latest window whose `effective_from` is on or before `trade_date`."""
        applicable = [
            window
            for window in self._windows[segment.value]
            if window.effective_from <= trade_date
        ]
        return applicable[-1] if applicable else None

    def segment_open(self, segment: Segment = Segment.FO, on: date | None = None) -> time:
        """Regular-session opening time (IST), used as the intraday bar anchor.

        Available regardless of source trust: session *timings* and the holiday
        *calendar* are separate facts, and TradingView anchors intraday bars on
        the session open whether or not a circular has been read.
        """
        window = self.window_for(on or date.today(), segment)
        if window is None:
            raise CalendarError(
                f"no {segment.value} session window is effective on {on}; "
                "the engine will not guess trading hours"
            )
        return window.regular_open

    def holidays(self, segment: Segment | None = None) -> tuple[CalendarEntry, ...]:
        return tuple(
            entry
            for entry in self._holidays
            if segment is None or entry.applies_to(segment)
        )

    def special_sessions(self, segment: Segment | None = None) -> tuple[CalendarEntry, ...]:
        return tuple(
            entry
            for entry in self._special
            if segment is None or entry.applies_to(segment)
        )

    def _match(
        self, entries: Iterable[CalendarEntry], trade_date: date, segment: Segment
    ) -> CalendarEntry | None:
        for entry in entries:
            if entry.date == trade_date and entry.applies_to(segment):
                return entry
        return None

    # --- classification ---------------------------------------------------

    def classify(self, moment: datetime, segment: Segment = Segment.FO) -> SessionStatus:
        """Classify a UTC instant against a segment's schedule."""
        if moment.tzinfo is None:
            raise ValueError("classify() requires a timezone-aware datetime")
        local = moment.astimezone(IST)
        trade_date = local.date()
        local_time = local.time().replace(second=0, microsecond=0)
        verified = self.covers(trade_date, segment)

        def status(state: SessionState, detail: str | None = None) -> SessionStatus:
            return SessionStatus(
                state=state,
                segment=segment,
                trade_date_ist=trade_date,
                local_time_ist=local_time,
                calendar_verified=verified,
                detail=detail,
            )

        # Special sessions are checked first, and before the weekend rule,
        # because overriding the ordinary calendar is their entire purpose:
        # NSE's 2026 Muhurat session is on Sunday 2026-11-08.
        special = self._match(self._special, trade_date, segment)
        if special is not None:
            if (
                special.source is not SourceTrust.OFFICIAL
                or special.regular_open is None
                or special.regular_close is None
            ):
                return status(
                    SessionState.UNSUPPORTED_SESSION,
                    f"{special.description} [source={special.source.value}]: "
                    "timings are not officially known, and the engine will not "
                    "assume regular hours",
                )
            if special.regular_open <= local_time < special.regular_close:
                if not verified:
                    return status(SessionState.UNVERIFIED_CALENDAR)
                return status(SessionState.OPEN, special.description)
            return status(SessionState.CLOSED, special.description)

        # Weekend is a property of the date, so it is reported whatever the
        # calendar's provenance.
        if local.weekday() >= 5:
            return status(SessionState.WEEKEND)

        # A holiday closes the day regardless of source trust: unofficial data
        # is allowed to close the market, only never to open it.
        holiday = self._match(self._holidays, trade_date, segment)
        if holiday is not None:
            return status(
                SessionState.HOLIDAY,
                f"{holiday.description} [source={holiday.source.value}]",
            )

        window = self.window_for(trade_date, segment)
        if window is None:
            return status(
                SessionState.UNSUPPORTED_SESSION,
                f"no {segment.value} session window is effective on {trade_date.isoformat()}",
            )

        # Everything from here can only yield OPEN/PRE_OPEN, so both the date
        # coverage and the window's own provenance must be official.
        if not verified:
            return status(
                SessionState.UNVERIFIED_CALENDAR,
                f"{segment.value} holidays_official_through="
                f"{self.holidays_official_through(segment)}",
            )
        if not window.is_official:
            return status(
                SessionState.UNVERIFIED_CALENDAR,
                f"{segment.value} session window effective "
                f"{window.effective_from.isoformat()} is marked "
                f"source={window.source.value}",
            )

        if window.pre_open_start is not None and window.pre_open_end is not None:
            if window.pre_open_start <= local_time < window.pre_open_end:
                return status(SessionState.PRE_OPEN)
        if window.regular_open <= local_time < window.regular_close:
            return status(SessionState.OPEN)
        return status(SessionState.CLOSED)
