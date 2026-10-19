from datetime import datetime, timezone

import pytest

from app.market_data.session import Segment, SessionCalendar, SessionState

# 04:40 UTC == 10:10 IST, inside the regular session.
TUESDAY_MIDSESSION = datetime(2026, 10, 20, 4, 40, tzinfo=timezone.utc)


def test_regular_session_is_open_on_a_verified_weekday(calendar_file):
    calendar = SessionCalendar.load(calendar_file)
    status = calendar.classify(TUESDAY_MIDSESSION, Segment.FO)
    assert status.state is SessionState.OPEN
    assert status.is_open
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
    calendar = SessionCalendar.load(calendar_file)
    # 10:00 UTC == 15:30 IST, the closing minute itself.
    assert calendar.classify(
        datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc), Segment.FO
    ).state is SessionState.CLOSED


def test_pre_open_window_is_distinct_from_regular_session(calendar_file):
    calendar = SessionCalendar.load(calendar_file)
    # 03:35 UTC == 09:05 IST, inside the cash pre-open window.
    assert calendar.classify(
        datetime(2026, 10, 20, 3, 35, tzinfo=timezone.utc), Segment.CASH
    ).state is SessionState.PRE_OPEN


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


def test_naive_timestamps_are_refused(calendar_file):
    calendar = SessionCalendar.load(calendar_file)
    with pytest.raises(ValueError):
        calendar.classify(datetime(2026, 10, 20, 4, 40))


def test_shipped_reference_calendar_is_unverified_until_the_owner_fills_it_in():
    # The repository must not ship invented NSE holiday data. Until the owner
    # populates it from official circulars, the calendar fails closed.
    from app.config import PROJECT_ROOT

    calendar = SessionCalendar.load(PROJECT_ROOT / "data" / "reference" / "nse_calendar.json")
    assert calendar.verified_through is None
    assert calendar.classify(TUESDAY_MIDSESSION, Segment.FO).state is (
        SessionState.UNVERIFIED_CALENDAR
    )
