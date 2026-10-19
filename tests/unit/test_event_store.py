from datetime import datetime, timezone

from app.signals.htr_adapter import SignalPolicy, normalize_alert
from app.signals.schemas import ReasonCode
from app.signals.worker import SignalWorker
from app.storage.event_store import EventStore, SignalStatus
from tests.conftest import INDICATOR, NOW, alert_payload

POLICY = SignalPolicy(
    expected_indicator=INDICATOR,
    allowed_symbols=("NSE:NIFTY",),
    allowed_timeframes=("1m", "3m", "5m", "15m"),
)


def make_event(**overrides):
    result = normalize_alert(alert_payload(**overrides), NOW, POLICY)
    assert result.accepted, result.reason
    return result.event


def store_event(store, event, correlation_id="corr-1"):
    raw_id = store.record_raw(
        correlation_id=correlation_id,
        received_at=NOW,
        body="{}",
        content_type="application/json",
    )
    return store.store_signal(
        event, correlation_id=correlation_id, raw_event_id=raw_id, session_state="OPEN"
    )


def test_signal_is_stored_once_and_queued_for_decision(tmp_path):
    store = EventStore(tmp_path / "engine.db")
    outcome = store_event(store, make_event())
    assert outcome.stored
    assert store.count_signals() == 1
    pending = store.pending_signals()
    assert len(pending) == 1
    assert pending[0]["signal_id"] == outcome.signal_id
    assert pending[0]["event"]["signal"] == "SELL"
    store.close()


def test_redelivery_of_the_same_bar_event_is_a_duplicate(tmp_path):
    store = EventStore(tmp_path / "engine.db")
    first = store_event(store, make_event())
    second = store_event(store, make_event())
    assert first.stored and not second.stored
    assert second.reason is ReasonCode.DUPLICATE_SIGNAL
    # The duplicate resolves to the original record, not a new one.
    assert second.signal_id == first.signal_id
    assert store.count_signals() == 1
    store.close()


def test_duplicates_are_still_detected_after_a_restart(tmp_path):
    path = tmp_path / "engine.db"
    first = EventStore(path)
    original = store_event(first, make_event())
    first.close()

    reopened = EventStore(path)
    repeat = store_event(reopened, make_event())
    assert not repeat.stored
    assert repeat.signal_id == original.signal_id
    assert reopened.count_signals() == 1
    reopened.close()


def test_pending_work_survives_a_restart(tmp_path):
    path = tmp_path / "engine.db"
    first = EventStore(path)
    outcome = store_event(first, make_event())
    first.close()

    reopened = EventStore(path)
    pending = reopened.pending_signals()
    assert [item["signal_id"] for item in pending] == [outcome.signal_id]
    reopened.close()


def test_opposite_directions_on_the_same_bar_are_separate_events(tmp_path):
    store = EventStore(tmp_path / "engine.db")
    assert store_event(store, make_event()).stored
    assert store_event(store, make_event(signal="BUY")).stored
    assert store.count_signals() == 2
    store.close()


def test_marking_decided_removes_an_event_from_the_queue(tmp_path):
    store = EventStore(tmp_path / "engine.db")
    outcome = store_event(store, make_event())
    store.mark_decided(outcome.signal_id)
    assert store.pending_signals() == []
    row = store.signal_by_key(make_event().idempotency_key)
    assert row["status"] == SignalStatus.DECIDED
    store.close()


def test_rejections_are_recorded_with_their_reason(tmp_path):
    store = EventStore(tmp_path / "engine.db")
    raw_id = store.record_raw(
        correlation_id="corr-2", received_at=NOW, body="{bad", content_type="text/plain"
    )
    store.record_rejection(
        correlation_id="corr-2",
        received_at=NOW,
        reason=ReasonCode.MALFORMED_JSON,
        detail="Expecting property name",
        raw_event_id=raw_id,
    )
    assert [row["reason"] for row in store.rejections()] == ["MALFORMED_JSON"]
    store.close()


def test_raw_bodies_are_retained_verbatim_for_audit(tmp_path):
    store = EventStore(tmp_path / "engine.db")
    body = '{"indicator":"Half Trend Regression [AlgoAlpha]"}'
    store.record_raw(
        correlation_id="corr-3", received_at=NOW, body=body, content_type="application/json"
    )
    row = store._conn.execute("SELECT body FROM raw_events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["body"] == body
    store.close()


def test_worker_reports_each_pending_signal_once(tmp_path):
    store = EventStore(tmp_path / "engine.db")
    store_event(store, make_event())
    worker = SignalWorker(store)
    assert len(worker.inspect_backlog()) == 1
    assert worker.inspect_backlog() == []
    store_event(store, make_event(signal="BUY"))
    assert len(worker.inspect_backlog()) == 1
    store.close()


def test_worker_does_not_decide_anything(tmp_path):
    # The behavioral engine is not implemented. Events must remain pending so
    # that no signal is silently treated as evaluated.
    store = EventStore(tmp_path / "engine.db")
    store_event(store, make_event())
    SignalWorker(store).inspect_backlog()
    pending = store.pending_signals()
    assert len(pending) == 1
    row = store.signal_by_key(make_event().idempotency_key)
    assert row["status"] == SignalStatus.PENDING_DECISION
    store.close()


def test_stored_timestamps_are_utc(tmp_path):
    store = EventStore(tmp_path / "engine.db")
    event = make_event()
    store_event(store, event)
    row = store.signal_by_key(event.idempotency_key)
    assert row["bar_close_time"].endswith("+00:00")
    assert datetime.fromisoformat(row["bar_close_time"]).tzinfo == timezone.utc
    store.close()
