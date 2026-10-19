"""NSE session classification.

Two rules this module exists to enforce:

1. Market-session logic is evaluated in `Asia/Kolkata`; every timestamp stored
   or returned is UTC.
2. Exchange schedules are data, never code. Holidays and session timings come
   from `data/reference/nse_calendar.json`. While that file's `verified_through`
   does not cover the timestamp being classified, the state is
   `UNVERIFIED_CALENDAR` rather than `OPEN` -- an unverified calendar must not
   be able to assert that the market was open.

Cash and derivatives segments are modelled separately because their timings and
holiday lists are not guaranteed to be identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Segment(StrEnum):
    CASH = "CASH"
    FO = "FO"


class SessionState(StrEnum):
    OPEN = "OPEN"
    PRE_OPEN = "PRE_OPEN"
    CLOSED = "CLOSED"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    UNVERIFIED_CALENDAR = "UNVERIFIED_CALENDAR"


@dataclass(frozen=True)
class SessionStatus:
    state: SessionState
    segment: Segment
    trade_date_ist: date
    local_time_ist: time
    calendar_verified: bool

    @property
    def is_open(self) -> bool:
        """True only when the calendar positively confirms a live regular session."""
        return self.state is SessionState.OPEN


def _parse_hhmm(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour=hour, minute=minute)


class SessionCalendar:
    def __init__(self, payload: dict) -> None:
        self._segments = payload["segments"]
        verified = payload.get("verified_through")
        self._verified_through = date.fromisoformat(verified) if verified else None
        self._holidays = {
            segment: {date.fromisoformat(d) for d in days}
            for segment, days in payload.get("trading_holidays", {}).items()
        }

    @classmethod
    def load(cls, path: Path) -> "SessionCalendar":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def verified_through(self) -> date | None:
        return self._verified_through

    def covers(self, trade_date: date) -> bool:
        return self._verified_through is not None and trade_date <= self._verified_through

    def classify(self, moment: datetime, segment: Segment = Segment.FO) -> SessionStatus:
        """Classify a UTC instant against the given segment's schedule."""
        if moment.tzinfo is None:
            raise ValueError("classify() requires a timezone-aware datetime")
        local = moment.astimezone(IST)
        trade_date = local.date()
        local_time = local.time().replace(second=0, microsecond=0)
        verified = self.covers(trade_date)

        def status(state: SessionState) -> SessionStatus:
            return SessionStatus(
                state=state,
                segment=segment,
                trade_date_ist=trade_date,
                local_time_ist=local_time,
                calendar_verified=verified,
            )

        # Weekend is a property of the date, not of the holiday list, so it is
        # reported even when the calendar file is unverified.
        if local.weekday() >= 5:
            return status(SessionState.WEEKEND)
        if trade_date in self._holidays.get(segment.value, set()):
            return status(SessionState.HOLIDAY)
        if not verified:
            return status(SessionState.UNVERIFIED_CALENDAR)

        timings = self._segments[segment.value]
        if pre_start := timings.get("pre_open_start"):
            if _parse_hhmm(pre_start) <= local_time < _parse_hhmm(timings["pre_open_end"]):
                return status(SessionState.PRE_OPEN)
        if _parse_hhmm(timings["regular_open"]) <= local_time < _parse_hhmm(
            timings["regular_close"]
        ):
            return status(SessionState.OPEN)
        return status(SessionState.CLOSED)
