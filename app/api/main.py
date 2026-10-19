"""FastAPI application factory.

The app wires the ingress to the event store, the contract policy and the
exchange calendar, and starts the observe-only signal worker. It exposes no
order-placement route, and `create_app` refuses to build an application whose
trading mode is anything other than PAPER.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI

from app.api.tradingview_webhook import router as tradingview_router
from app.config import Settings, get_settings
from app.logging_setup import configure_logging
from app.market_data.session import Segment, SessionCalendar
from app.signals.htr_adapter import policy_from_settings
from app.signals.worker import SignalWorker
from app.storage.event_store import EventStore

logger = logging.getLogger("app")


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    if settings.trading_mode != "PAPER":
        raise RuntimeError("V1 runs in PAPER mode only; there is no live-order code path")
    if not settings.webhook_token:
        raise RuntimeError(
            "WEBHOOK_TOKEN is not set. Generate 32+ random characters and put it in .env"
        )

    # The webhook token is registered as a secret so that no log line can
    # carry it, whichever library writes the line.
    configure_logging(secrets=(settings.webhook_token,))
    store = EventStore(settings.database_path)
    calendar = SessionCalendar.load(settings.holiday_file)
    worker = SignalWorker(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task: asyncio.Task | None = None
        if start_worker:
            task = asyncio.create_task(worker.run(stop))
        logger.info(
            "engine started",
            extra={
                "mode": settings.trading_mode,
                "live_orders": False,
                "symbols": list(settings.symbol_allowlist),
                "timeframes": list(settings.timeframe_allowlist),
                "calendar_verified_through": str(calendar.verified_through),
            },
        )
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            store.close()

    app = FastAPI(
        title="NIFTY Behavioral Options Engine",
        version="0.1.0",
        description="Paper-trading research engine. Records and evaluates Half Trend "
        "Regression signals. Places no broker orders.",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.event_store = store
    app.state.session_calendar = calendar
    app.state.signal_policy = policy_from_settings(
        settings, session_open_ist=calendar.segment_open(Segment.FO)
    )
    app.state.webhook_token = settings.webhook_token
    app.state.max_body_bytes = settings.max_body_bytes
    app.state.signal_worker = worker
    app.state.clock = lambda: datetime.now(tz=timezone.utc)

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "mode": settings.trading_mode,
            "live_orders": False,
            "signals_recorded": store.count_signals(),
            "pending_decisions": len(store.pending_signals(limit=1000)),
            "decision_engine": "NOT_IMPLEMENTED",
            "exchange_calendar_verified_through": str(calendar.verified_through),
            "exchange_calendar_holiday_coverage": calendar.holiday_coverage,
            "schema_versions": list(store.schema_versions),
        }

    @app.get("/v1/telemetry/signals")
    def signal_telemetry(limit: int = 50) -> dict:
        """Read-only transport telemetry for recorded signals.

        Exists so delivery latency and candle alignment can be inspected without
        opening the database by hand. It reads; it cannot act.
        """
        return {
            "mode": settings.trading_mode,
            "live_orders": False,
            "budgets": {
                "max_tradable_age_seconds": settings.max_tradable_age_seconds,
                "max_recordable_age_seconds": settings.max_recordable_age_seconds,
                "clock_skew_tolerance_seconds": settings.clock_skew_tolerance_seconds,
                "bar_close_tolerance_seconds": settings.bar_close_tolerance_seconds,
            },
            "signals": store.telemetry(limit=max(1, min(limit, 500))),
        }

    app.include_router(tradingview_router)
    return app
