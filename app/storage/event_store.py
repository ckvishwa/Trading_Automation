"""Durable event storage (SQLite, WAL).

The webhook handler must be able to acknowledge a TradingView alert only after
the event is on disk, so every write here is a committed transaction before the
caller regains control. Exactly-once semantics come from a primary key on the
signal idempotency key: a re-delivered alert loses the INSERT race and is
reported as a duplicate rather than processed twice.

Webhook credentials are never written. Request bodies are stored; the
authentication token travels in the URL path and headers, neither of which is
persisted or logged.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.signals.schemas import NormalizedSignalEvent, ReasonCode

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    content_type  TEXT,
    body          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_events (
    idempotency_key TEXT PRIMARY KEY,
    signal_id       TEXT NOT NULL UNIQUE,
    correlation_id  TEXT NOT NULL,
    raw_event_id    INTEGER NOT NULL REFERENCES raw_events(id),
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    signal          TEXT NOT NULL,
    bar_open_time   TEXT NOT NULL,
    bar_close_time  TEXT NOT NULL,
    fired_at        TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    signal_price    REAL NOT NULL,
    mode            TEXT NOT NULL,
    session_state   TEXT NOT NULL,
    status          TEXT NOT NULL,
    event_json      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS signal_events_status_idx
    ON signal_events (status, received_at);

CREATE TABLE IF NOT EXISTS ingest_rejections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    reason         TEXT NOT NULL,
    detail         TEXT,
    raw_event_id   INTEGER
);
"""


class SignalStatus:
    PENDING_DECISION = "PENDING_DECISION"
    DECIDED = "DECIDED"


@dataclass(frozen=True)
class StoreOutcome:
    stored: bool
    signal_id: str
    reason: ReasonCode | None = None


class EventStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock: writes are short, ordering matters,
        # and a single writer avoids SQLITE_BUSY retries entirely.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        # executescript() commits any open transaction before it runs, so the
        # DDL cannot be wrapped in this class's explicit transaction helper.
        with self._lock:
            self._conn.executescript(_SCHEMA)
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now().astimezone().isoformat()),
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- writes -----------------------------------------------------------

    def record_raw(
        self,
        *,
        correlation_id: str,
        received_at: datetime,
        body: str,
        content_type: str | None,
        source: str = "tradingview",
    ) -> int:
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO raw_events (correlation_id, received_at, source, content_type, body)"
                " VALUES (?, ?, ?, ?, ?)",
                (correlation_id, received_at.isoformat(), source, content_type, body),
            )
            return int(cursor.lastrowid)

    def record_rejection(
        self,
        *,
        correlation_id: str,
        received_at: datetime,
        reason: ReasonCode,
        detail: str | None,
        raw_event_id: int | None,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO ingest_rejections"
                " (correlation_id, received_at, reason, detail, raw_event_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (correlation_id, received_at.isoformat(), reason.value, detail, raw_event_id),
            )

    def store_signal(
        self,
        event: NormalizedSignalEvent,
        *,
        correlation_id: str,
        raw_event_id: int,
        session_state: str,
    ) -> StoreOutcome:
        """Persist a normalized event exactly once.

        Returns stored=False with the original signal_id when the same bar
        event has already been recorded.
        """
        signal_id = str(uuid.uuid4())
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO signal_events ("
                " idempotency_key, signal_id, correlation_id, raw_event_id, symbol, timeframe,"
                " signal, bar_open_time, bar_close_time, fired_at, received_at, signal_price,"
                " mode, session_state, status, event_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    event.idempotency_key,
                    signal_id,
                    correlation_id,
                    raw_event_id,
                    event.symbol,
                    event.timeframe,
                    event.signal.value,
                    event.bar_open_time.isoformat(),
                    event.bar_close_time.isoformat(),
                    event.fired_at.isoformat(),
                    event.received_at.isoformat(),
                    event.signal_price,
                    event.mode,
                    session_state,
                    SignalStatus.PENDING_DECISION,
                    event.model_dump_json(),
                ),
            )
            if cursor.rowcount == 1:
                return StoreOutcome(stored=True, signal_id=signal_id)
            existing = conn.execute(
                "SELECT signal_id FROM signal_events WHERE idempotency_key = ?",
                (event.idempotency_key,),
            ).fetchone()
        return StoreOutcome(
            stored=False,
            signal_id=existing["signal_id"],
            reason=ReasonCode.DUPLICATE_SIGNAL,
        )

    def mark_decided(self, signal_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE signal_events SET status = ? WHERE signal_id = ?",
                (SignalStatus.DECIDED, signal_id),
            )

    # --- reads ------------------------------------------------------------

    def pending_signals(self, limit: int = 50) -> list[dict]:
        """Durable work queue: events awaiting a behavioral decision.

        Reading from the table rather than an in-process queue is what makes
        restart recovery and replay possible without losing events.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT signal_id, event_json, session_state FROM signal_events"
                " WHERE status = ? ORDER BY received_at LIMIT ?",
                (SignalStatus.PENDING_DECISION, limit),
            ).fetchall()
        return [
            {
                "signal_id": row["signal_id"],
                "session_state": row["session_state"],
                "event": json.loads(row["event_json"]),
            }
            for row in rows
        ]

    def count_signals(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM signal_events").fetchone()
        return int(row["n"])

    def signal_by_key(self, idempotency_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM signal_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return dict(row) if row else None

    def rejections(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason, detail, received_at FROM ingest_rejections ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]
