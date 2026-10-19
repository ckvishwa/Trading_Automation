"""The webhook token must not reach a log, a database row, or a response body.

TradingView cannot send custom headers, so the shared secret is the last path
segment. That makes an access log a credential leak, and makes "nothing logs
paths" a control that has to be tested rather than assumed.
"""

from __future__ import annotations

import io
import logging

import pytest
from fastapi.testclient import TestClient

from app import __main__ as entrypoint
from app.api.main import create_app
from app.logging_setup import REDACTED, SecretRedactingFilter, configure_logging
from tests.conftest import NOW, TEST_TOKEN, alert_payload

SECRET = "s" * 48


def record(message: str, *args, **extra) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args or None,
        exc_info=None,
        **({"extra": extra} if False else {}),
    )


def test_access_logging_is_disabled_at_the_entrypoint():
    # Not a preference: an access log would write the token on every delivery.
    assert entrypoint.ACCESS_LOG_ENABLED is False


def test_the_filter_redacts_a_secret_from_the_message():
    log_record = record(f"POST /v1/webhooks/tradingview/{SECRET}")
    SecretRedactingFilter([SECRET]).filter(log_record)
    assert SECRET not in log_record.getMessage()
    assert REDACTED in log_record.getMessage()


def test_the_filter_redacts_a_secret_from_format_arguments():
    log_record = record("path %s", SECRET)
    SecretRedactingFilter([SECRET]).filter(log_record)
    assert SECRET not in log_record.getMessage()


def test_the_filter_redacts_a_secret_from_structured_fields():
    log_record = record("request")
    log_record.url = f"/alert/{SECRET}"
    SecretRedactingFilter([SECRET]).filter(log_record)
    assert log_record.url == f"/alert/{REDACTED}"


def test_the_filter_leaves_unrelated_text_alone():
    log_record = record("signal recorded for NSE:NIFTY")
    SecretRedactingFilter([SECRET]).filter(log_record)
    assert log_record.getMessage() == "signal recorded for NSE:NIFTY"


def test_short_strings_are_not_treated_as_secrets():
    # Redacting a short value would scrub ordinary words out of every message.
    log_record = record("mode PAPER")
    SecretRedactingFilter(["PAPER"]).filter(log_record)
    assert log_record.getMessage() == "mode PAPER"


def test_configure_logging_installs_the_filter_without_duplicating_it():
    configure_logging(secrets=(SECRET,), stream=io.StringIO())
    handler = configure_logging(secrets=(SECRET,), stream=io.StringIO())
    installed = [f for f in handler.filters if isinstance(f, SecretRedactingFilter)]
    assert len(installed) == 1


def test_the_filter_is_applied_to_handlers_not_to_the_root_logger():
    # A filter on a logger is applied only to records logged on that logger, not
    # to records propagated from its children -- which is most of them. Pinning
    # this stops a future refactor from quietly moving it.
    handler = configure_logging(secrets=(SECRET,), stream=io.StringIO())
    assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)
    assert not any(
        isinstance(f, SecretRedactingFilter) for f in logging.getLogger().filters
    )


@pytest.fixture
def client(settings):
    app = create_app(settings, start_worker=False)
    app.state.clock = lambda: NOW
    with TestClient(app) as test_client:
        yield test_client


def test_no_log_line_from_a_real_request_contains_the_token(client):
    """Checked against what the configured handler actually writes.

    Capturing the handler's own stream is the only honest test here: a separate
    capture handler added by a test framework carries no redaction, so asserting
    on that would be asserting about the harness rather than the engine.
    """
    stream = io.StringIO()
    configure_logging(secrets=(TEST_TOKEN,), stream=stream)
    client.post(f"/v1/webhooks/tradingview/{TEST_TOKEN}", json=alert_payload())
    client.post("/v1/webhooks/tradingview/wrong-token", json=alert_payload())
    written = stream.getvalue()
    assert "signal recorded" in written
    assert TEST_TOKEN not in written


def test_a_third_party_logger_that_logs_the_url_is_still_redacted():
    """The realistic leak, and the reason the filter sits on the handler.

    An HTTP client library logging its own request line is exactly how this
    token escapes, and that record is propagated from a child logger -- which a
    filter on the root *logger* would never see. Observed for real: httpx logs
    the full request URL at INFO.
    """
    stream = io.StringIO()
    configure_logging(secrets=(TEST_TOKEN,), stream=stream)
    logging.getLogger("httpx").info(
        "HTTP Request: POST http://host/v1/webhooks/tradingview/%s 200 OK", TEST_TOKEN
    )
    written = stream.getvalue()
    assert TEST_TOKEN not in written
    assert REDACTED in written


def test_no_response_body_echoes_the_token(client):
    accepted = client.post(
        f"/v1/webhooks/tradingview/{TEST_TOKEN}", json=alert_payload()
    )
    rejected = client.post("/v1/webhooks/tradingview/wrong-token", json=alert_payload())
    assert TEST_TOKEN not in accepted.text
    assert TEST_TOKEN not in rejected.text


def test_no_stored_row_contains_the_token(client):
    client.post(f"/v1/webhooks/tradingview/{TEST_TOKEN}", json=alert_payload())
    store = client.app.state.event_store
    tables = ("raw_events", "signal_events", "ingest_rejections")
    for table in tables:
        rows = store._conn.execute(f"SELECT * FROM {table}").fetchall()
        for row in rows:
            assert TEST_TOKEN not in str(dict(row))


def test_the_telemetry_endpoint_does_not_expose_the_token(client):
    client.post(f"/v1/webhooks/tradingview/{TEST_TOKEN}", json=alert_payload())
    body = client.get("/v1/telemetry/signals")
    assert TEST_TOKEN not in body.text
