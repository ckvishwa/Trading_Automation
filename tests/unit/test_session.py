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


def test_shipped_calendar_has_official_holidays_for_both_segments(shipped):
    """Holidays are official for both segments; timings are official too.

    The holiday lists come from NSE's own holiday-master API (CM and FO). The
    session timings are the published NSE windows, including the 2026-08-03
    extension. What is NOT official is the Muhurat session's timings, which NSE
    has not yet notified -- that stays UNSUPPORTED_SESSION.
    """
    assert shipped.holidays_official_through(Segment.FO) == date(2026, 12, 31)
    assert shipped.holidays_official_through(Segment.CASH) == date(2026, 12, 31)
    assert all(
        window.source is SourceTrust.OFFICIAL
        for segment in (Segment.FO, Segment.CASH)
        for window in shipped.windows(segment)
    )


def test_shipped_calendar_records_the_sources_it_came_from(shipped):
    official = shipped.sources["official_retrieved"]
    # The holiday-master API, the corroborating F&O circular, and the session
    # extension are all recorded with their provenance.
    apis = [item for item in official if "holiday-master" in item.get("url", "")]
    assert apis and len(apis[0]["sha256"]) == 64
    circulars = [item for item in official if item.get("circular") == "NSE/FAOP/71777"]
    assert circulars and circulars[0]["circular_date"] == "2025-12-12"
    # The session-timing page could not be byte-captured: that is recorded
    # honestly rather than claimed as retrieved.
    extension = [item for item in official if item.get("effective_from") == "2026-08-03"]
    assert extension and "NOT byte-captured" in extension[0]["retrieved"]
    # What remains outstanding is named, not quietly assumed.
    outstanding = " ".join(item["what"] for item in shipped.sources["still_needed"])
    assert "session-timing" in outstanding
    assert "Muhurat" in outstanding


def test_shipped_calendar_reports_open_during_an_official_session(shipped):
    # 2026-09-07 is a Monday after the extension; 10:00 IST == 04:30 UTC.
    status = shipped.classify(
        datetime(2026, 9, 7, 4, 30, tzinfo=timezone.utc), Segment.FO
    )
    assert status.state is SessionState.OPEN
    assert status.is_open and status.may_trade


def test_the_fo_session_extends_to_1540_only_from_2026_08_03(shipped):
    """Task 1's core check, both sides of the effective date.

    15:35 IST == 10:05 UTC. Before 2026-08-03 the F&O session closed 15:30, so
    15:35 is CLOSED; on/after it closes 15:40, so 15:35 is OPEN. The rule change
    is effective-dated, never applied retroactively.
    """
    before = datetime(2026, 7, 6, 10, 5, tzinfo=timezone.utc)  # Mon pre-extension
    after = datetime(2026, 9, 7, 10, 5, tzinfo=timezone.utc)  # Mon post-extension
    assert shipped.classify(before, Segment.FO).state is SessionState.CLOSED
    assert shipped.classify(after, Segment.FO).state is SessionState.OPEN
    # 15:41 IST == 10:11 UTC is past even the extended close.
    just_after = datetime(2026, 9, 7, 10, 11, tzinfo=timezone.utc)
    assert shipped.classify(just_after, Segment.FO).state is SessionState.CLOSED
    # The window selected for a post-extension date closes at 15:40.
    window = shipped.window_for(date(2026, 9, 7), Segment.FO)
    assert window.regular_close == time(15, 40)
    assert window.effective_from == date(2026, 8, 3)


def test_the_cash_segment_keeps_its_1530_close(shipped):
    # The extension is a derivatives change; the cash continuous session is
    # unchanged at 15:30 (CAS is modelled as metadata, not a new close).
    window = shipped.window_for(date(2026, 9, 7), Segment.CASH)
    assert window.regular_close == time(15, 30)
    # 15:35 IST == 10:05 UTC: open for F&O, closed for cash on the same day.
    moment = datetime(2026, 9, 7, 10, 5, tzinfo=timezone.utc)
    assert shipped.classify(moment, Segment.FO).state is SessionState.OPEN
    assert shipped.classify(moment, Segment.CASH).state is SessionState.CLOSED


def test_shipped_calendar_closes_2026_10_20_officially_for_both_segments(shipped):
    """Dussehra, Tuesday 2026-10-20 -- in both the API and the circular."""
    for segment in (Segment.FO, Segment.CASH):
        status = shipped.classify(TUESDAY_MIDSESSION, segment)
        assert status.state is SessionState.HOLIDAY
        assert "Dussehra" in status.detail
        assert "source=official" in status.detail
        assert status.calendar_verified is True
        assert not status.is_open


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


def test_the_api_lists_twenty_official_fo_holidays_including_weekend_ones(shipped):
    entries = shipped.holidays(Segment.FO)
    # The holiday-master API returns 20 for 2026 -- the circular's 18 plus the
    # 15-Jan Municipal Election and the 08-Nov Diwali Laxmi Pujan Sunday.
    assert len(entries) == 20
    weekend = {
        entry.date.isoformat() for entry in entries if entry.date.weekday() >= 5
    }
    assert {"2026-02-15", "2026-03-21", "2026-08-15", "2026-11-08"} <= weekend
    assert all(entry.source is SourceTrust.OFFICIAL for entry in entries)


def test_the_cash_and_fo_holiday_lists_are_both_official_and_match_for_2026(shipped):
    cash = {entry.date.isoformat() for entry in shipped.holidays(Segment.CASH)}
    fo = {entry.date.isoformat() for entry in shipped.holidays(Segment.FO)}
    assert cash == fo
    assert len(cash) == 20


def test_muhurat_is_the_official_date_with_unknown_timings(shipped):
    """2026-11-08 is a Sunday, and NSE says so deliberately.

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


def test_muhurat_is_the_only_special_session_and_the_old_guess_is_gone(shipped):
    # An earlier revision carried a third-party 2026-10-21 guess. NSE settles
    # it: Muhurat is 2026-11-08.
    special_dates = {entry.date.isoformat() for entry in shipped.special_sessions()}
    assert special_dates == {"2026-11-08"}


def test_shipped_calendar_still_exposes_a_bar_grid_anchor(shipped):
    # Session timings and the holiday calendar are separate facts: the intraday
    # bar anchor is needed even while the timings are unverified.
    assert shipped.segment_open(Segment.FO, on=date(2026, 10, 19)) == time(9, 15)
