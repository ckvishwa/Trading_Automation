"""Contract tests for the TradingView ingress.

These run against the real FastAPI app with a real SQLite store in a temp
directory. Nothing is mocked except the clock, which is frozen so that
freshness and session logic are deterministic.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from tests.conftest import NOW, TEST_TOKEN, alert_payload

PATH = f"/v1/webhooks/tradingview/{TEST_TOKEN}"


@pytest.fixture
def client(settings):
    app = create_app(settings, start_worker=False)
    app.state.clock = lambda: NOW
    with TestClient(app) as test_client:
        yield test_client


def post(client, payload, path=PATH, **kwargs):
    return client.post(path, json=payload, **kwargs)


def test_valid_sell_signal_is_recorded(client):
    response = post(client, alert_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SIGNAL_RECORDED"
    assert body["signal"] == "SELL"
    assert body["symbol"] == "NSE:NIFTY"
    assert body["timeframe"] == "5m"
    assert body["bar_close_time"] == "2026-10-20T04:40:00+00:00"
    assert body["mode"] == "PAPER"
    assert body["session_state"] == "OPEN"
    assert "No order" in body["note"]


def test_valid_buy_signal_is_recorded(client):
    body = post(client, alert_payload(signal="BUY")).json()
    assert body["status"] == "SIGNAL_RECORDED"
    assert body["signal"] == "BUY"


def test_both_directions_are_stored_exactly_once(client):
    post(client, alert_payload(signal="BUY"))
    post(client, alert_payload(signal="SELL"))
    assert client.get("/health").json()["signals_recorded"] == 2


def test_missing_signal_field_is_rejected(client):
    payload = alert_payload()
    del payload["signal"]
    body = post(client, payload).json()
    assert body["status"] == "REJECTED"
    assert body["reason"] == "SCHEMA_INVALID"


def test_unsupported_symbol_is_rejected(client):
    body = post(client, alert_payload(ticker="BANKNIFTY")).json()
    assert body["reason"] == "UNSUPPORTED_SYMBOL"


def test_unsupported_timeframe_is_rejected(client):
    body = post(client, alert_payload(interval="7")).json()
    assert body["reason"] == "UNSUPPORTED_TIMEFRAME"


def test_unexpected_indicator_is_rejected(client):
    body = post(client, alert_payload(indicator="Supertrend")).json()
    assert body["reason"] == "UNEXPECTED_INDICATOR"


def test_non_reversal_event_is_rejected(client):
    body = post(client, alert_payload(event="TREND_STILL_RED")).json()
    assert body["reason"] == "NOT_A_REVERSAL_EVENT"


def test_duplicate_delivery_is_rejected_and_stores_nothing_new(client):
    first = post(client, alert_payload()).json()
    second = post(client, alert_payload()).json()
    assert second["status"] == "REJECTED"
    assert second["reason"] == "DUPLICATE_SIGNAL"
    assert second["signal_id"] == first["signal_id"]
    assert client.get("/health").json()["signals_recorded"] == 1


def test_stale_event_is_rejected(client, settings):
    app_client = client
    # Alert fired 04:40Z, received 04:45Z: beyond the 120s delivery budget.
    app_client.app.state.clock = lambda: NOW.replace(minute=45, second=0)
    body = post(app_client, alert_payload()).json()
    assert body["reason"] == "STALE_SIGNAL"


def test_future_timestamp_is_rejected(client):
    body = post(client, alert_payload(fired_at="2026-10-20T05:40:00Z")).json()
    assert body["reason"] == "FUTURE_TIMESTAMP"


def test_malformed_json_is_rejected_with_400(client):
    response = client.post(PATH, content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["reason"] == "MALFORMED_JSON"


def test_unauthorized_request_is_rejected_with_401(client):
    response = client.post("/v1/webhooks/tradingview/wrong-token", json=alert_payload())
    assert response.status_code == 401
    assert response.json()["reason"] == "UNAUTHORIZED"


def test_unauthorized_request_stores_nothing(client):
    client.post("/v1/webhooks/tradingview/wrong-token", json=alert_payload())
    store = client.app.state.event_store
    assert store.count_signals() == 0
    assert store.rejections() == []
    row = store._conn.execute("SELECT COUNT(*) AS n FROM raw_events").fetchone()
    assert row["n"] == 0


def test_header_token_is_accepted_for_local_tooling(client):
    response = client.post(
        "/v1/webhooks/tradingview",
        json=alert_payload(),
        headers={"X-Webhook-Token": TEST_TOKEN},
    )
    assert response.json()["status"] == "SIGNAL_RECORDED"


def test_missing_header_token_is_unauthorized(client):
    response = client.post("/v1/webhooks/tradingview", json=alert_payload())
    assert response.status_code == 401


def test_oversized_body_is_rejected_with_413(client):
    padded = alert_payload(note="x" * 20_000)
    response = client.post(PATH, content=json.dumps(padded).encode(), headers={
        "content-type": "application/json"
    })
    assert response.status_code == 413
    assert response.json()["reason"] == "PAYLOAD_TOO_LARGE"


def test_every_rejection_is_durably_recorded_with_a_reason(client):
    post(client, alert_payload(ticker="BANKNIFTY"))
    post(client, alert_payload(interval="7"))
    reasons = [row["reason"] for row in client.app.state.event_store.rejections()]
    assert reasons == ["UNSUPPORTED_SYMBOL", "UNSUPPORTED_TIMEFRAME"]


def test_raw_payload_is_recorded_before_acknowledgement(client):
    post(client, alert_payload())
    store = client.app.state.event_store
    row = store._conn.execute("SELECT body FROM raw_events ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(row["body"])["ticker"] == "NIFTY"


def test_no_webhook_token_is_ever_persisted(client):
    post(client, alert_payload())
    store = client.app.state.event_store
    bodies = store._conn.execute("SELECT body FROM raw_events").fetchall()
    assert all(TEST_TOKEN not in row["body"] for row in bodies)


def test_accepted_signal_is_queued_for_a_decision_not_traded(client):
    body = post(client, alert_payload()).json()
    pending = client.app.state.event_store.pending_signals()
    assert [item["signal_id"] for item in pending] == [body["signal_id"]]
    health = client.get("/health").json()
    assert health["pending_decisions"] == 1
    assert health["decision_engine"] == "NOT_IMPLEMENTED"


def test_health_reports_paper_mode_and_no_live_orders(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "PAPER"
    assert body["live_orders"] is False


def test_application_exposes_no_order_routes(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert not any("order" in path or "trade" in path for path in paths)
    assert paths == {"/health", "/v1/webhooks/tradingview", "/v1/webhooks/tradingview/{token}"}


def test_restart_replays_pending_work_and_still_rejects_duplicates(settings):
    """Restart behaviour: a committed event survives, and its redelivery is still a duplicate."""
    first_app = create_app(settings, start_worker=False)
    first_app.state.clock = lambda: NOW
    with TestClient(first_app) as first_client:
        original = post(first_client, alert_payload()).json()

    second_app = create_app(settings, start_worker=False)
    second_app.state.clock = lambda: NOW
    with TestClient(second_app) as second_client:
        pending = second_app.state.event_store.pending_signals()
        assert [item["signal_id"] for item in pending] == [original["signal_id"]]

        repeat = post(second_client, alert_payload()).json()
        assert repeat["reason"] == "DUPLICATE_SIGNAL"
        assert repeat["signal_id"] == original["signal_id"]
        assert second_client.get("/health").json()["signals_recorded"] == 1


def test_app_refuses_to_start_without_a_webhook_token(settings):
    naked = settings.model_copy(update={"webhook_token": ""})
    with pytest.raises(RuntimeError, match="WEBHOOK_TOKEN"):
        create_app(naked)


def test_app_refuses_any_mode_other_than_paper(settings):
    # trading_mode is typed as Literal["PAPER"], so a live mode cannot even be
    # constructed from configuration; the guard covers programmatic misuse.
    live = settings.model_copy(update={"trading_mode": "LIVE"})
    with pytest.raises(RuntimeError, match="PAPER"):
        create_app(live)
