import json
from datetime import date, datetime, time, timezone

import pytest

from app.market_data.session import (
    CalendarError,
    Segment,
    SessionCalendar,
    SessionState,
    SourceTrust,
)

# 04:40 UTC == 10:10 IST, inside the regular session. 2026-10-20 is a Tuesday.
TUESDAY_MIDSESSION = datetime(2026, 10, 20, 4, 40, tzinfo=timezone.utc)


def test_regular_session_is_open_under_an_official_verified_calendar(calendar_file):
    calendar = SessionCalendar.load(calendar_file)
    status = calendar.classify(TUESDAY_MIDSESSION, Segment.FO)
    assert status.state is SessionState.OPEN
    assert status.is_open and status.may_trade
    assert status.trade_date_ist.isoformat() == "2026-10-20"
    assert status.local_time_ist.isoformat() == "10:10:00"


def test_before_and_after_hours_are_closed(calendar_file):
    calendar = SessionCalendar.load(calendar_file)
    # 03:00 UTC == 08:30 IST, 11:00 UTC == 16:30 IST.
    assert calendar.classify(
        datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc), Segment.FO
    ).state is SessionState.CLOSED
    assert calendar.classify(
        datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc), Segment.FO
    ).state is SessionState.CLOSED


def test_close_time_is_exclusive(calendar_file):
    # 10:00 UTC == 15:30 IST, the closing minute itself.
    calendar = SessionCalendar.load(calendar_file)
    assert calendar.classify(
        datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc), Segment.FO
    ).state is SessionState.CLOSED


def test_pre_open_window_is_distinct_from_regular_session(calendar_file):
    # 03:35 UTC == 09:05 IST, inside the cash pre-open window.
    calendar = SessionCalendar.load(calendar_file)
    status = calendar.classify(datetime(2026, 10, 20, 3, 35, tzinfo=timezone.utc), Segment.CASH)
    assert status.state is SessionState.PRE_OPEN
    assert not status.is_open


def test_weekend_is_reported_even_without_a_verified_calendar(unverified_calendar_file):
    calendar = SessionCalendar.load(unverified_calendar_file)
    sunday = datetime(2026, 10, 18, 4, 40, tzinfo=timezone.utc)
    assert calendar.classify(sunday, Segment.FO).state is SessionState.WEEKEND


def test_segments_do_not_share_a_holiday_list(calendar_file):
    calendar = SessionCalendar.load(calendar_file)
    wednesday = datetime(2026, 10, 21, 4, 40, tzinfo=timezone.utc)
    assert calendar.classify(wednesday, Segment.CASH).state is SessionState.HOLIDAY
    assert calendar.classify(wednesday, Segment.FO).state is SessionState.OPEN


def test_unverified_calendar_never_claims_the_market_was_open(unverified_calendar_file):
    calendar = SessionCalendar.load(unverified_calendar_file)
    status = calendar.classify(TUESDAY_MIDSESSION, Segment.FO)
    assert status.state is SessionState.UNVERIFIED_CALENDAR
    assert not status.calendar_verified
    assert not status.is_open


def test_dates_beyond_the_verified_horizon_are_unverified(calendar_file):
    calendar = SessionCalendar.load(calendar_file)
    beyond = datetime(2027, 3, 2, 4, 40, tzinfo=timezone.utc)
    assert calendar.classify(beyond, Segment.FO).state is SessionState.UNVERIFIED_CALENDAR


def test_unofficial_session_timings_cannot_open_the_market(unofficial_window_calendar_file):
    """The asymmetry that matters.

    Dates are covered, but the timings came from a third party. Being wrongly
    closed costs a missed trade; being wrongly open costs a trade placed into a
    market that was not there.
    """
    calendar = SessionCalendar.load(unofficial_window_calendar_file)
    wednesday = datetime(2026, 10, 22, 4, 40, tzinfo=timezone.utc)
    status = calendar.classify(wednesday, Segment.FO)
    assert status.state is SessionState.UNVERIFIED_CALENDAR
    assert "source=unofficial" in status.detail
    assert not status.is_open


def test_an_unofficial_holiday_may_still_close_the_market(unofficial_window_calendar_file):
    calendar = SessionCalendar.load(unofficial_window_calendar_file)
    status = calendar.classify(TUESDAY_MIDSESSION, Segment.FO)
    assert status.state is SessionState.HOLIDAY
    assert "source=unofficial" in status.detail


def test_a_disputed_special_session_is_unsupported_not_assumed(special_session_calendar_file):
    calendar = SessionCalendar.load(special_session_calendar_file)
    status = calendar.classify(
        datetime(2026, 10, 21, 4, 40, tzinfo=timezone.utc), Segment.FO
    )
    assert status.state is SessionState.UNSUPPORTED_SESSION
    assert "will not" in status.detail
    assert not status.is_open


def test_an_official_special_session_uses_its_own_timings(special_session_calendar_file):
    calendar = SessionCalendar.load(special_session_calendar_file)
    # 2026-11-10, 18:15-19:15 IST == 12:45-13:45 UTC. Regular hours are long over.
    assert calendar.classify(
        datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc), Segment.FO
    ).state is SessionState.OPEN
    assert calendar.classify(
        datetime(2026, 11, 10, 4, 40, tzinfo=timezone.utc), Segment.FO
    ).state is SessionState.CLOSED


def test_effective_dated_windows_select_the_one_in_force(tmp_path):
    payload = {
        "schema_version": 2,
        "official_verified_through": "2026-12-31",
        "segments": {
            "FO": {
                "sessions": [
                    {
                        "effective_from": "2000-01-01",
                        "regular_open": "09:15",
                        "regular_close": "15:30",
                        "source": "official",
                    },
                    {
                        "effective_from": "2026-11-01",
                        "regular_open": "09:00",
                        "regular_close": "17:00",
                        "source": "official",
                    },
                ]
            }
        },
    }
    path = tmp_path / "effective.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    calendar = SessionCalendar.load(path)

    assert calendar.segment_open(Segment.FO, on=date(2026, 10, 20)) == time(9, 15)
    assert calendar.segment_open(Segment.FO, on=date(2026, 11, 2)) == time(9, 0)
    # 10:45 UTC == 16:15 IST: closed under the old window, open under the new one.
    assert calendar.classify(
        datetime(2026, 10, 20, 10, 45, tzinfo=timezone.utc), Segment.FO
    ).state is SessionState.CLOSED
    assert calendar.classify(
        datetime(2026, 11, 2, 10, 45, tzinfo=timezone.utc), Segment.FO
    ).state is SessionState.OPEN


def test_a_date_before_every_window_is_unsupported_not_guessed(tmp_path):
    payload = {
        "schema_version": 2,
        "official_verified_through": "2026-12-31",
        "segments": {
            "FO": {
                "sessions": [
                    {
                        "effective_from": "2026-11-01",
                        "regular_open": "09:15",
                        "regular_close": "15:30",
                        "source": "official",
                    }
                ]
            }
        },
    }
    path = tmp_path / "late.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    calendar = SessionCalendar.load(path)
    status = calendar.classify(TUESDAY_MIDSESSION, Segment.FO)
    assert status.state is SessionState.UNSUPPORTED_SESSION
    with pytest.raises(CalendarError, match="will not guess"):
        calendar.segment_open(Segment.FO, on=date(2026, 10, 20))


def test_an_unsupported_schema_version_is_refused(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"verified_through": None, "segments": {}}), encoding="utf-8")
    with pytest.raises(CalendarError, match="schema_version"):
        SessionCalendar.load(path)


def test_naive_timestamps_are_refused(calendar_file):
    calendar = SessionCalendar.load(calendar_file)
    with pytest.raises(ValueError):
        calendar.classify(datetime(2026, 10, 20, 4, 40))


# --- the calendar actually shipped in the repository ---------------------------


@pytest.fixture(scope="module")
def shipped() -> SessionCalendar:
    from app.config import PROJECT_ROOT

    return SessionCalendar.load(PROJECT_ROOT / "data" / "reference" / "nse_calendar.json")


def test_shipped_calendar_claims_no_official_verification(shipped):
    # The repository must not ship invented or third-party-sourced data as if it
    # had been read from a circular.
    assert shipped.verified_through is None
    assert shipped.holiday_coverage == "partial"
    assert all(
        window.source is SourceTrust.UNOFFICIAL for window in shipped.windows(Segment.FO)
    )


def test_shipped_calendar_never_reports_open(shipped):
    for moment in (
        TUESDAY_MIDSESSION,
        datetime(2026, 10, 19, 4, 40, tzinfo=timezone.utc),
        datetime(2026, 6, 10, 6, 0, tzinfo=timezone.utc),
    ):
        assert not shipped.classify(moment, Segment.FO).is_open


def test_shipped_calendar_closes_2026_10_20_for_both_segments(shipped):
    """Dussehra, Tuesday 2026-10-20.

    Flagged as third-party sourced, which is enough to close the day but not to
    open any other. See the `_WARNING` in the calendar file.
    """
    for segment in (Segment.CASH, Segment.FO):
        status = shipped.classify(TUESDAY_MIDSESSION, segment)
        assert status.state is SessionState.HOLIDAY
        assert "Dussehra" in status.detail
        assert "source=unofficial" in status.detail
        assert not status.is_open


def test_shipped_calendar_marks_the_disputed_muhurat_date_unsupported(shipped):
    status = shipped.classify(
        datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc), Segment.FO
    )
    assert status.state is SessionState.UNSUPPORTED_SESSION
    assert "DISPUTED" in status.detail


def test_shipped_calendar_still_exposes_a_bar_grid_anchor(shipped):
    # Session timings and the holiday calendar are separate facts: the intraday
    # bar anchor is needed even while the holiday list is unverified.
    assert shipped.segment_open(Segment.FO, on=date(2026, 10, 19)) == time(9, 15)
