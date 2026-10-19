"""Replay regression tests.

Replay exists so that a historical run and a live run cannot disagree: both go
through `normalize_alert` and the same `EventStore`. These tests check that the
replay path is deterministic, honours ordering, and collapses duplicates exactly
as the HTTP path does.
"""

from __future__ import annotations

import json
from datetime import time, timedelta

import pytest

from app.market_data.session import SessionCalendar
from app.research.replay import SignalReplay, read_jsonl
from app.signals.htr_adapter import SignalPolicy
from app.signals.schemas import ReasonCode
from app.storage.event_store import EventStore
from tests.conftest import INDICATOR, alert_payload

POLICY = SignalPolicy(
    expected_indicator=INDICATOR,
    allowed_symbols=("NSE:NIFTY",),
    allowed_timeframes=("1m", "3m", "5m", "15m"),
    max_signal_age_seconds=120,
    clock_skew_tolerance_seconds=10,
    session_open_ist=time(9, 15),
)


def bar(minutes_after_open: int, signal: str = "SELL") -> dict:
    """An alert for the 5m bar that opens `minutes_after_open` after 09:15 IST."""
    from datetime import datetime, timezone

    session_open = datetime(2026, 10, 20, 3, 45, tzinfo=timezone.utc)
    bar_open = session_open + timedelta(minutes=minutes_after_open)
    fired = bar_open + timedelta(minutes=5)
    return alert_payload(
        signal=signal,
        bar_open_time=bar_open.isoformat().replace("+00:00", "Z"),
        fired_at=fired.isoformat().replace("+00:00", "Z"),
    )


@pytest.fixture
def replay(tmp_path, calendar_file):
    store = EventStore(tmp_path / "replay.db")
    calendar = SessionCalendar.load(calendar_file)
    yield SignalReplay(store, POLICY, calendar), store
    store.close()


def test_a_clean_run_records_every_event_once(replay):
    engine, store = replay
    summary = engine.run([bar(0), bar(5, "BUY"), bar(10)])
    assert summary.as_dict() == {
        "total": 3,
        "recorded": 3,
        "duplicates": 0,
        "rejected": 0,
        "out_of_order": 0,
        "reasons": {},
    }
    assert store.count_signals() == 3


def test_duplicates_collapse_exactly_as_over_http(replay):
    engine, store = replay
    summary = engine.run([bar(0), bar(0), bar(0)])
    assert summary.recorded == 1
    assert summary.duplicates == 2
    assert summary.reasons[ReasonCode.DUPLICATE_SIGNAL.value] == 2
    assert store.count_signals() == 1


def test_events_are_replayed_in_fired_at_order_not_file_order(replay):
    engine, _ = replay
    summary = engine.run([bar(10), bar(0), bar(5)])
    order = [event.bar_close_time for event in summary.events]
    assert order == sorted(order)
    # Reordering for replay is not the same as hiding the disorder.
    assert summary.out_of_order == 0


def test_the_replay_clock_comes_from_the_event_not_from_wall_time(replay):
    engine, _ = replay
    # Alerts from months ago replay cleanly; against a wall clock they would all
    # be rejected as STALE_SIGNAL.
    summary = engine.run([bar(0), bar(5)])
    assert summary.rejected == 0
    assert summary.recorded == 2


def test_delivery_delay_beyond_the_freshness_budget_is_rejected(tmp_path, calendar_file):
    store = EventStore(tmp_path / "slow.db")
    engine = SignalReplay(
        store,
        POLICY,
        SessionCalendar.load(calendar_file),
        delivery_delay=timedelta(minutes=10),
    )
    summary = engine.run([bar(0)])
    assert summary.rejected == 1
    assert summary.reasons[ReasonCode.STALE_SIGNAL.value] == 1
    store.close()


def test_contract_violations_are_tallied_by_reason(replay):
    engine, store = replay
    summary = engine.run(
        [
            bar(0),
            alert_payload(ticker="BANKNIFTY"),
            alert_payload(interval="7"),
            alert_payload(indicator="Supertrend"),
        ]
    )
    assert summary.recorded == 1
    assert summary.rejected == 3
    assert summary.reasons == {
        "UNSUPPORTED_SYMBOL": 1,
        "UNSUPPORTED_TIMEFRAME": 1,
        "UNEXPECTED_INDICATOR": 1,
    }
    assert len(store.rejections()) == 3


def test_payloads_without_a_timestamp_are_replayed_last_and_rejected(replay):
    engine, _ = replay
    broken = alert_payload()
    del broken["fired_at"]
    summary = engine.run([broken, bar(0)])
    assert summary.recorded == 1
    assert summary.reasons[ReasonCode.SCHEMA_INVALID.value] == 1
    assert summary.events[-1].reason is ReasonCode.SCHEMA_INVALID


def test_replaying_the_same_input_twice_into_fresh_stores_is_identical(tmp_path, calendar_file):
    payloads = [bar(0), bar(5, "BUY"), bar(0), alert_payload(ticker="BANKNIFTY")]
    calendar = SessionCalendar.load(calendar_file)
    summaries = []
    for run in range(2):
        store = EventStore(tmp_path / f"run{run}.db")
        summaries.append(SignalReplay(store, POLICY, calendar).run(payloads).as_dict())
        store.close()
    assert summaries[0] == summaries[1]


def test_replaying_into_a_store_that_already_has_the_events_records_nothing_new(replay):
    engine, store = replay
    first = engine.run([bar(0), bar(5, "BUY")])
    second = engine.run([bar(0), bar(5, "BUY")])
    assert first.recorded == 2
    assert second.recorded == 0
    assert second.duplicates == 2
    assert store.count_signals() == 2


def test_replayed_events_are_queued_for_a_decision_like_live_ones(replay):
    engine, store = replay
    engine.run([bar(0)])
    pending = store.pending_signals()
    assert len(pending) == 1
    assert pending[0]["event"]["signal"] == "SELL"


def test_raw_payloads_are_stored_with_a_replay_source(replay):
    engine, store = replay
    engine.run([bar(0)])
    row = store._conn.execute("SELECT source, body FROM raw_events").fetchone()
    assert row["source"] == "replay"
    assert json.loads(row["body"])["indicator"] == INDICATOR


def test_jsonl_round_trip(tmp_path, replay):
    engine, _ = replay
    path = tmp_path / "signals.jsonl"
    path.write_text(
        "\n".join(json.dumps(payload) for payload in [bar(0), bar(5, "BUY")]) + "\n",
        encoding="utf-8",
    )
    summary = engine.run_jsonl(path)
    assert summary.recorded == 2


def test_a_malformed_jsonl_line_raises_instead_of_being_skipped(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="broken.jsonl:2"):
        list(read_jsonl(path))


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "padded.jsonl"
    path.write_text('\n{"a": 1}\n\n', encoding="utf-8")
    assert list(read_jsonl(path)) == [{"a": 1}]
