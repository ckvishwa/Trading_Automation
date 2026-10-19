"""TradingView webhook ingress.

Design constraints that shape this module:

* TradingView posts to a fixed URL and cannot attach custom headers, so the
  shared secret is carried as the final path segment and compared in constant
  time. A header is also accepted so that local tooling and tests do not have
  to put the secret in a URL.
* TradingView cancels a delivery that takes longer than three seconds, so the
  handler does exactly two things: validate the contract, and commit the event.
  All analysis happens later, off the request path, driven by the durable
  PENDING_DECISION queue.
* The event is on disk before the acknowledgement is produced. A 200 means
  "recorded", never "will be traded".
* No order-placement code exists in this package, and no configuration value
  can introduce one.
"""

from __future__ import annotations

import hmac
import json
import logging
import time as _time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request, Response
from starlette.concurrency import run_in_threadpool

from app.market_data.session import Segment
from app.signals.htr_adapter import normalize_alert
from app.signals.schemas import ReasonCode

logger = logging.getLogger("webhook.tradingview")

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

ACCEPTED = "SIGNAL_RECORDED"
REJECTED = "REJECTED"


def _json_response(status_code: int, payload: dict) -> Response:
    return Response(
        content=json.dumps(payload),
        status_code=status_code,
        media_type="application/json",
        headers={"cache-control": "no-store"},
    )


def _token_matches(supplied: str | None, expected: str) -> bool:
    if not expected or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


async def _handle(
    request: Request, supplied_token: str | None, header_token: str | None
) -> Response:
    state = request.app.state
    correlation_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    # Injected clock: freshness and session logic must be reproducible in tests
    # and in historical replay, not tied to wall-clock time.
    clock = getattr(state, "clock", None)
    now = clock() if clock is not None else datetime.now(tz=timezone.utc)
    # Monotonic, so a wall-clock adjustment mid-request cannot produce a
    # negative processing time.
    started = _time.perf_counter()

    def elapsed_ms() -> int:
        return round((_time.perf_counter() - started) * 1000)

    authorized = _token_matches(supplied_token, state.webhook_token) or _token_matches(
        header_token, state.webhook_token
    )
    if not authorized:
        # Nothing is stored for an unauthenticated caller: an open endpoint that
        # writes to disk on every request is a denial-of-service surface.
        logger.warning(
            "unauthorized webhook request",
            extra={
                "correlation_id": correlation_id,
                "reason": ReasonCode.UNAUTHORIZED.value,
                "processing_ms": elapsed_ms(),
            },
        )
        return _json_response(401, {"status": REJECTED, "reason": ReasonCode.UNAUTHORIZED.value})

    body = await request.body()
    if len(body) > state.max_body_bytes:
        logger.warning(
            "webhook body too large",
            extra={
                "correlation_id": correlation_id,
                "reason": ReasonCode.PAYLOAD_TOO_LARGE.value,
                "bytes": len(body),
            },
        )
        return _json_response(
            413, {"status": REJECTED, "reason": ReasonCode.PAYLOAD_TOO_LARGE.value}
        )

    text = body.decode("utf-8", errors="replace")
    content_type = request.headers.get("content-type")
    store = state.event_store

    raw_event_id = await run_in_threadpool(
        store.record_raw,
        correlation_id=correlation_id,
        received_at=now,
        body=text,
        content_type=content_type,
    )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        await run_in_threadpool(
            store.record_rejection,
            correlation_id=correlation_id,
            received_at=now,
            reason=ReasonCode.MALFORMED_JSON,
            detail=str(exc),
            raw_event_id=raw_event_id,
            processing_ms=elapsed_ms(),
        )
        logger.warning(
            "malformed webhook JSON",
            extra={"correlation_id": correlation_id, "reason": ReasonCode.MALFORMED_JSON.value},
        )
        return _json_response(
            400, {"status": REJECTED, "reason": ReasonCode.MALFORMED_JSON.value}
        )

    result = normalize_alert(payload, now, state.signal_policy)
    if not result.accepted:
        await run_in_threadpool(
            store.record_rejection,
            correlation_id=correlation_id,
            received_at=now,
            reason=result.reason,
            detail=result.detail,
            raw_event_id=raw_event_id,
            processing_ms=elapsed_ms(),
        )
        logger.info(
            "signal rejected at ingress",
            extra={
                "correlation_id": correlation_id,
                "reason": result.reason.value,
                "detail": result.detail,
                "received_at": now.isoformat(),
                "processing_ms": elapsed_ms(),
            },
        )
        return _json_response(
            200,
            {"status": REJECTED, "reason": result.reason.value, "detail": result.detail},
        )

    event = result.event
    session_status = state.session_calendar.classify(event.bar_close_time, Segment.FO)
    processed_at = (clock() if clock is not None else datetime.now(tz=timezone.utc))
    outcome = await run_in_threadpool(
        store.store_signal,
        event,
        correlation_id=correlation_id,
        raw_event_id=raw_event_id,
        session_state=session_status.state.value,
        processed_at=processed_at,
        processing_ms=elapsed_ms(),
    )

    if not outcome.stored:
        logger.info(
            "duplicate signal ignored",
            extra={
                "correlation_id": correlation_id,
                "reason": ReasonCode.DUPLICATE_SIGNAL.value,
                "signal_id": outcome.signal_id,
                "idempotency_key": event.idempotency_key,
                "processing_ms": elapsed_ms(),
            },
        )
        return _json_response(
            200,
            {
                "status": REJECTED,
                "reason": ReasonCode.DUPLICATE_SIGNAL.value,
                "signal_id": outcome.signal_id,
            },
        )

    # End-to-end transport telemetry, one structured line per recorded signal.
    # Every timestamp the pipeline saw is here, so delivery latency and candle
    # alignment can be reconstructed without re-deriving anything.
    logger.info(
        "signal recorded",
        extra={
            "correlation_id": correlation_id,
            "signal_id": outcome.signal_id,
            "symbol": event.symbol,
            "timeframe": event.timeframe,
            "signal": event.signal.value,
            "bar_open_time": event.bar_open_time.isoformat(),
            "bar_close_time": event.bar_close_time.isoformat(),
            "fired_at": event.fired_at.isoformat(),
            "received_at": event.received_at.isoformat(),
            "processed_at": processed_at.isoformat(),
            "delivery_delay_ms": event.delivery_delay_ms,
            "bar_close_lag_ms": event.bar_close_lag_ms,
            "processing_ms": elapsed_ms(),
            "freshness": event.freshness.value,
            "tradable": event.tradable,
            "session_state": session_status.state.value,
            "mode": event.mode,
        },
    )
    return _json_response(
        200,
        {
            "status": ACCEPTED,
            "signal_id": outcome.signal_id,
            "idempotency_key": event.idempotency_key,
            "signal": event.signal.value,
            "symbol": event.symbol,
            "timeframe": event.timeframe,
            "bar_open_time": event.bar_open_time.isoformat(),
            "bar_close_time": event.bar_close_time.isoformat(),
            "fired_at": event.fired_at.isoformat(),
            "received_at": event.received_at.isoformat(),
            "delivery_delay_ms": event.delivery_delay_ms,
            "bar_close_lag_ms": event.bar_close_lag_ms,
            "processing_ms": elapsed_ms(),
            "freshness": event.freshness.value,
            "tradable": event.tradable,
            "session_state": session_status.state.value,
            "mode": event.mode,
            "note": "Recorded for behavioral evaluation. No order, fill, or trade decision."
            + ("" if event.tradable else " Delivered too late to trade; analysis only."),
        },
    )


@router.post("/tradingview/{token}")
async def receive_with_path_token(
    token: str,
    request: Request,
    x_webhook_token: str | None = Header(default=None),
) -> Response:
    return await _handle(request, token, x_webhook_token)


@router.post("/tradingview")
async def receive_with_header_token(
    request: Request,
    x_webhook_token: str | None = Header(default=None),
) -> Response:
    return await _handle(request, None, x_webhook_token)
