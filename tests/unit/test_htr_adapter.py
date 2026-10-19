from datetime import datetime, time, timezone

import pytest

from app.signals.htr_adapter import SignalPolicy, normalize_alert
from app.signals.schemas import ReasonCode, SignalDirection
from tests.conftest import INDICATOR, NOW, alert_payload

POLICY = SignalPolicy(
    expected_indicator=INDICATOR,
    allowed_symbols=("NSE:NIFTY",),
    allowed_timeframes=("1m", "3m", "5m", "15m"),
    max_signal_age_seconds=120,
    clock_skew_tolerance_seconds=10,
)


def normalize(payload, now=NOW, policy=POLICY):
    return normalize_alert(payload, now, policy)


def test_accepts_valid_sell_signal_and_derives_bar_close():
    result = normalize(alert_payload())
    assert result.accepted
    event = result.event
    assert event.signal is SignalDirection.SELL
    assert event.symbol == "NSE:NIFTY"
    assert event.timeframe == "5m"
    assert event.bar_open_time == datetime(2026, 10, 20, 4, 35, tzinfo=timezone.utc)
    assert event.bar_close_time == datetime(2026, 10, 20, 4, 40, tzinfo=timezone.utc)
    assert event.signal_price == pytest.approx(24000.5)
    assert event.mode == "PAPER"


def test_accepts_valid_buy_signal():
    result = normalize(alert_payload(signal="BUY"))
    assert result.accepted
    assert result.event.signal is SignalDirection.BUY


@pytest.mark.parametrize("wording", ["BULLISH", "green", "LONG", "up"])
def test_legacy_bullish_wordings_map_to_buy(wording):
    # Alerts already configured on the user's chart used BULLISH/BEARISH.
    result = normalize(alert_payload(signal=wording))
    assert result.accepted
    assert result.event.signal is SignalDirection.BUY


@pytest.mark.parametrize("wording", ["BEARISH", "red", "SHORT", "down"])
def test_legacy_bearish_wordings_map_to_sell(wording):
    result = normalize(alert_payload(signal=wording))
    assert result.accepted
    assert result.event.signal is SignalDirection.SELL


def test_legacy_field_names_still_validate():
    legacy = {
        "indicator": INDICATOR,
        "event": "REVERSAL",
        "direction": "BEARISH",
        "exchange": "NSE",
        "ticker": "NIFTY",
        "interval": "5",
        "index_close": "24000.5",
        "bar_time": "2026-10-20T04:35:00Z",
        "fired_at": "2026-10-20T04:40:00Z",
    }
    result = normalize(legacy)
    assert result.accepted
    assert result.event.signal is SignalDirection.SELL


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"signal": None}, ReasonCode.SCHEMA_INVALID),
        ({"indicator": "Supertrend"}, ReasonCode.UNEXPECTED_INDICATOR),
        ({"event": "TREND_STILL_RED"}, ReasonCode.NOT_A_REVERSAL_EVENT),
        ({"signal": "SIDEWAYS"}, ReasonCode.UNKNOWN_SIGNAL_DIRECTION),
        ({"ticker": "BANKNIFTY"}, ReasonCode.UNSUPPORTED_SYMBOL),
        ({"exchange": "BSE", "ticker": "SENSEX"}, ReasonCode.UNSUPPORTED_SYMBOL),
        ({"interval": "7"}, ReasonCode.UNSUPPORTED_TIMEFRAME),
        ({"interval": "D"}, ReasonCode.UNSUPPORTED_TIMEFRAME),
        ({"interval": "banana"}, ReasonCode.UNSUPPORTED_TIMEFRAME),
        ({"signal_price": "0"}, ReasonCode.INVALID_SIGNAL_PRICE),
        ({"signal_price": "-1"}, ReasonCode.INVALID_SIGNAL_PRICE),
        ({"signal_price": "not-a-number"}, ReasonCode.INVALID_SIGNAL_PRICE),
        ({"mode": "LIVE"}, ReasonCode.MODE_NOT_PAPER),
        ({"bar_open_time": "not-a-time"}, ReasonCode.SCHEMA_INVALID),
    ],
)
def test_contract_violations_name_their_reason(overrides, reason):
    result = normalize(alert_payload(**overrides))
    assert not result.accepted
    assert result.reason is reason


def test_missing_required_field_is_schema_invalid():
    payload = alert_payload()
    del payload["ticker"]
    result = normalize(payload)
    assert result.reason is ReasonCode.SCHEMA_INVALID
    assert "ticker" in result.detail


def test_stale_delivery_is_rejected():
    late = datetime(2026, 10, 20, 4, 45, tzinfo=timezone.utc)  # 5 minutes after firing
    result = normalize(alert_payload(), now=late)
    assert result.reason is ReasonCode.STALE_SIGNAL


def test_delivery_inside_the_age_budget_is_accepted():
    just_in_time = datetime(2026, 10, 20, 4, 41, 59, tzinfo=timezone.utc)
    assert normalize(alert_payload(), now=just_in_time).accepted


def test_future_fired_at_is_rejected():
    result = normalize(alert_payload(fired_at="2026-10-20T05:40:00Z"))
    assert result.reason is ReasonCode.FUTURE_TIMESTAMP


def test_future_bar_open_is_rejected():
    result = normalize(
        alert_payload(bar_open_time="2026-10-20T05:35:00Z", fired_at="2026-10-20T04:40:00Z")
    )
    assert result.reason is ReasonCode.FUTURE_TIMESTAMP


def test_alert_that_fired_before_its_bar_closed_is_rejected():
    # bar_open 04:38 on a 5m chart closes at 04:43, but the alert claims it
    # fired at 04:40 -- an unconfirmed, potentially repainting bar.
    result = normalize(
        alert_payload(bar_open_time="2026-10-20T04:38:00Z", fired_at="2026-10-20T04:40:00Z")
    )
    assert result.reason is ReasonCode.BAR_CLOSE_AFTER_FIRE_TIME


def test_clock_skew_tolerance_is_applied_not_ignored():
    # Fired 8 seconds "in the future" is within the 10s tolerance.
    result = normalize(alert_payload(fired_at="2026-10-20T04:40:38Z"))
    assert result.accepted


@pytest.mark.parametrize("payload", [None, [], "SELL", 42])
def test_non_object_bodies_are_schema_invalid(payload):
    assert normalize(payload).reason is ReasonCode.SCHEMA_INVALID


def test_idempotency_key_is_stable_across_redeliveries():
    first = normalize(alert_payload()).event
    # Same bar event, re-delivered later with a new {{timenow}}.
    second = normalize(
        alert_payload(fired_at="2026-10-20T04:40:45Z"),
        now=datetime(2026, 10, 20, 4, 41, tzinfo=timezone.utc),
    ).event
    assert first.idempotency_key == second.idempotency_key


def test_idempotency_key_separates_direction_and_bar():
    sell = normalize(alert_payload()).event
    buy = normalize(alert_payload(signal="BUY")).event
    later = normalize(
        alert_payload(bar_open_time="2026-10-20T04:40:00Z", fired_at="2026-10-20T04:45:00Z"),
        now=datetime(2026, 10, 20, 4, 45, 20, tzinfo=timezone.utc),
    ).event
    assert len({sell.idempotency_key, buy.idempotency_key, later.idempotency_key}) == 3


ALIGNED_POLICY = SignalPolicy(
    expected_indicator=INDICATOR,
    allowed_symbols=("NSE:NIFTY",),
    allowed_timeframes=("1m", "3m", "5m", "10m", "15m"),
    session_open_ist=time(9, 15),
)


def test_bar_aligned_to_the_session_open_grid_is_accepted():
    # 04:35Z == 10:05 IST == 50 minutes after the 09:15 open == ten 5m bars.
    assert normalize(alert_payload(), policy=ALIGNED_POLICY).accepted


def test_first_bar_of_the_session_is_accepted():
    # 03:45Z == 09:15 IST, the session's opening bar, offset zero.
    result = normalize(
        alert_payload(bar_open_time="2026-10-20T03:45:00Z", fired_at="2026-10-20T03:50:00Z"),
        now=datetime(2026, 10, 20, 3, 50, 20, tzinfo=timezone.utc),
        policy=ALIGNED_POLICY,
    )
    assert result.accepted


def test_bar_off_the_timeframe_grid_is_rejected():
    # 04:37Z == 10:07 IST == 52 minutes after the open: not a whole 5m bar.
    result = normalize(
        alert_payload(bar_open_time="2026-10-20T04:37:00Z", fired_at="2026-10-20T04:42:00Z"),
        now=datetime(2026, 10, 20, 4, 42, 20, tzinfo=timezone.utc),
        policy=ALIGNED_POLICY,
    )
    assert result.reason is ReasonCode.BAR_NOT_ALIGNED_TO_TIMEFRAME


def test_bar_before_the_session_open_is_rejected():
    # 03:35Z == 08:45 IST, before the session opens.
    result = normalize(
        alert_payload(bar_open_time="2026-10-20T03:35:00Z", fired_at="2026-10-20T03:40:00Z"),
        now=datetime(2026, 10, 20, 3, 40, 20, tzinfo=timezone.utc),
        policy=ALIGNED_POLICY,
    )
    assert result.reason is ReasonCode.BAR_NOT_ALIGNED_TO_TIMEFRAME


def test_alignment_anchors_on_the_session_open_not_utc_midnight():
    # A 10m NSE bar opens at 09:15, 09:25, ... IST. 04:35Z == 10:05 IST is 50
    # minutes after the open, so it IS a valid 10m bar -- even though 10:05 IST
    # (04:35 UTC) is not on a 10-minute grid measured from UTC midnight.
    result = normalize(
        alert_payload(interval="10", fired_at="2026-10-20T04:45:00Z"),
        now=datetime(2026, 10, 20, 4, 45, 20, tzinfo=timezone.utc),
        policy=ALIGNED_POLICY,
    )
    assert result.accepted
    assert result.event.timeframe == "10m"


def test_alignment_check_is_skipped_when_no_anchor_is_configured():
    # Without a session-open anchor the check is disabled rather than guessed.
    off_grid = alert_payload(
        bar_open_time="2026-10-20T04:37:00Z", fired_at="2026-10-20T04:42:00Z"
    )
    now = datetime(2026, 10, 20, 4, 42, 20, tzinfo=timezone.utc)
    assert POLICY.session_open_ist is None
    assert normalize(off_grid, now=now).accepted
