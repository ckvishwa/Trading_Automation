import json
from datetime import date, datetime, time, timezone

import pytest

from app.market_data.session import (
    IST,
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
        "schema_version": 3,
        "segments": {
            "FO": {
                "holidays_official_through": "2026-12-31",
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
                ],
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
        "schema_version": 3,
        "segments": {
            "FO": {
                "holidays_official_through": "2026-12-31",
                "sessions": [
                    {
                        "effective_from": "2026-11-01",
                        "regular_open": "09:15",
                        "regular_close": "15:30",
                        "source": "official",
                    }
                ],
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


@pytest.mark.parametrize("version", [None, 1, 2, 4])
def test_an_unsupported_schema_version_is_refused(tmp_path, version):
    path = tmp_path / "old.json"
    payload = {"segments": {}}
    if version is not None:
        payload["schema_version"] = version
    path.write_text(json.dumps(payload), encoding="utf-8")
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


def test_shipped_calendar_has_official_fo_holidays_and_nothing_else_official(shipped):
    """What the circular does and does not establish.

    NSE/FAOP/71777 gives the 2026 F&O holiday list. It states no session
    timings, and it says nothing about the cash segment. So the F&O holiday
    horizon is official while the timings stay unofficial and the cash horizon
    stays null -- an official holiday list must not be allowed to vouch for
    either.
    """
    assert shipped.holidays_official_through(Segment.FO) == date(2026, 12, 31)
    assert shipped.holidays_official_through(Segment.CASH) is None
    assert all(
        window.source is SourceTrust.UNOFFICIAL
        for segment in (Segment.FO, Segment.CASH)
        for window in shipped.windows(segment)
    )


def test_shipped_calendar_records_the_circular_it_came_from(shipped):
    official = shipped.sources["official_retrieved"]
    assert len(official) == 1
    circular = official[0]
    assert circular["circular"] == "NSE/FAOP/71777"
    assert circular["circular_date"] == "2025-12-12"
    assert circular["segment"] == "FO"
    assert circular["url"].endswith("/FAOP71777.pdf")
    assert len(circular["sha256"]) == 64
    assert circular["retrieved_at_utc"].endswith("Z")
    # The cash list and the session timings are recorded as outstanding, not
    # quietly assumed.
    outstanding = " ".join(item["what"] for item in shipped.sources["still_needed"])
    assert "Capital Market" in outstanding
    assert "session timings" in outstanding
    assert "Muhurat" in outstanding


def test_shipped_calendar_never_reports_open(shipped):
    # Timings are unofficial, so no ordinary day can be open whatever the
    # holiday list says.
    for moment in (
        TUESDAY_MIDSESSION,
        datetime(2026, 10, 19, 4, 40, tzinfo=timezone.utc),
        datetime(2026, 6, 10, 6, 0, tzinfo=timezone.utc),
    ):
        for segment in (Segment.FO, Segment.CASH):
            assert not shipped.classify(moment, segment).is_open


def test_an_official_holiday_list_does_not_make_the_timings_official(shipped):
    # 2026-10-19 is an ordinary Monday inside the official F&O holiday horizon.
    status = shipped.classify(
        datetime(2026, 10, 19, 4, 40, tzinfo=timezone.utc), Segment.FO
    )
    assert status.calendar_verified is True  # the holiday list covers the date
    assert status.state is SessionState.UNVERIFIED_CALENDAR  # the timings do not
    assert "source=unofficial" in status.detail


def test_shipped_calendar_closes_2026_10_20_officially_for_fo(shipped):
    """Dussehra, Tuesday 2026-10-20 -- entry 12 of the circular's table."""
    status = shipped.classify(TUESDAY_MIDSESSION, Segment.FO)
    assert status.state is SessionState.HOLIDAY
    assert "Dussehra" in status.detail
    assert "source=official" in status.detail
    assert status.calendar_verified is True
    assert not status.is_open


def test_shipped_calendar_also_closes_2026_10_20_for_cash_but_unofficially(shipped):
    # The cash circular has not been read. A third-party entry may close the
    # day; it may never open one, and it does not claim verification.
    status = shipped.classify(TUESDAY_MIDSESSION, Segment.CASH)
    assert status.state is SessionState.HOLIDAY
    assert "source=unofficial" in status.detail
    assert status.calendar_verified is False


@pytest.mark.parametrize(
    ("day", "description"),
    [
        ((2026, 1, 26), "Republic Day"),
        ((2026, 3, 3), "Holi"),
        ((2026, 3, 26), "Shri Ram Navami"),
        ((2026, 3, 31), "Shri Mahavir Jayanti"),
        ((2026, 4, 3), "Good Friday"),
        ((2026, 4, 14), "Ambedkar Jayanti"),
        ((2026, 5, 1), "Maharashtra Day"),
        ((2026, 5, 28), "Bakri Id"),
        ((2026, 6, 26), "Muharram"),
        ((2026, 9, 14), "Ganesh Chaturthi"),
        ((2026, 10, 2), "Mahatma Gandhi Jayanti"),
        ((2026, 10, 20), "Dussehra"),
        ((2026, 11, 10), "Diwali-Balipratipada"),
        ((2026, 11, 24), "Guru Nanak"),
        ((2026, 12, 25), "Christmas"),
    ],
)
def test_every_weekday_holiday_in_the_circular_closes_the_fo_segment(
    shipped, day, description
):
    status = shipped.classify(
        datetime(*day, 4, 40, tzinfo=timezone.utc), Segment.FO
    )
    assert status.state is SessionState.HOLIDAY, f"{date(*day)} {description}"
    assert "source=official" in status.detail


def test_the_circular_lists_fifteen_weekday_and_three_weekend_fo_holidays(shipped):
    entries = shipped.holidays(Segment.FO)
    assert len(entries) == 18
    weekend = [entry for entry in entries if entry.date.weekday() >= 5]
    assert {entry.date.isoformat() for entry in weekend} == {
        "2026-02-15",
        "2026-03-21",
        "2026-08-15",
    }
    assert all(entry.source is SourceTrust.OFFICIAL for entry in entries)


def test_muhurat_is_the_official_date_with_unofficial_timings(shipped):
    """2026-11-08 is a Sunday, and the circular says so deliberately.

    The date is official; the timings are explicitly "notified subsequently".
    A special session is therefore checked before the weekend rule -- otherwise
    this would report WEEKEND and the pending session would be invisible -- and
    it reports UNSUPPORTED_SESSION rather than assuming regular hours.
    """
    moment = datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc)
    assert moment.astimezone(IST).weekday() == 6  # Sunday
    for segment in (Segment.FO, Segment.CASH):
        status = shipped.classify(moment, segment)
        assert status.state is SessionState.UNSUPPORTED_SESSION
        assert "Muhurat" in status.detail
        assert "notified subsequently" in status.detail
        assert not status.is_open


def test_the_third_party_muhurat_date_guess_is_gone(shipped):
    # An earlier revision carried 2026-10-21 from a broker page. The circular
    # settles it: Muhurat is 2026-11-08.
    special_dates = {entry.date.isoformat() for entry in shipped.special_sessions()}
    assert special_dates == {"2026-11-08"}


def test_shipped_calendar_still_exposes_a_bar_grid_anchor(shipped):
    # Session timings and the holiday calendar are separate facts: the intraday
    # bar anchor is needed even while the timings are unverified.
    assert shipped.segment_open(Segment.FO, on=date(2026, 10, 19)) == time(9, 15)
