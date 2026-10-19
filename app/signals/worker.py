"""Signal event worker.

The worker exists so the HTTP handler never carries analysis work. It reads the
durable PENDING_DECISION queue rather than an in-process queue, which is what
makes the pipeline restart-safe: events committed before a crash are still
waiting afterwards.

In this milestone the worker only observes the backlog. The behavioral engine
(M4) is not implemented, so nothing here accepts, rejects, or decides anything,
and events deliberately stay in PENDING_DECISION until a real decision stage
exists to move them.
"""

from __future__ import annotations

import asyncio
import logging

from app.storage.event_store import EventStore

logger = logging.getLogger("worker.signals")


class SignalWorker:
    def __init__(self, store: EventStore, poll_seconds: float = 5.0, batch: int = 50) -> None:
        self._store = store
        self._poll_seconds = poll_seconds
        self._batch = batch
        self._reported: set[str] = set()

    def inspect_backlog(self) -> list[dict]:
        """Return pending events, newest signal ids first time they are seen.

        Separated from the loop so the behaviour is testable without timers.
        """
        pending = self._store.pending_signals(limit=self._batch)
        fresh = [item for item in pending if item["signal_id"] not in self._reported]
        for item in fresh:
            self._reported.add(item["signal_id"])
        return fresh

    async def run(self, stop: asyncio.Event) -> None:
        logger.info(
            "signal worker started",
            extra={"stage": "observe_only", "decision_engine": "NOT_IMPLEMENTED"},
        )
        while not stop.is_set():
            try:
                for item in await asyncio.to_thread(self.inspect_backlog):
                    event = item["event"]
                    logger.info(
                        "signal awaiting behavioral decision",
                        extra={
                            "signal_id": item["signal_id"],
                            "symbol": event["symbol"],
                            "timeframe": event["timeframe"],
                            "signal": event["signal"],
                            "bar_close_time": event["bar_close_time"],
                            "session_state": item["session_state"],
                            "decision_engine": "NOT_IMPLEMENTED",
                        },
                    )
            except Exception:
                # Logged, never swallowed silently; the loop must survive a bad
                # row so that later events are still observed.
                logger.exception("signal worker iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("signal worker stopped")
