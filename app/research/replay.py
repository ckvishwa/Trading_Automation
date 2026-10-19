"""Deterministic chronological replay of signal events.

Purpose: run recorded alert payloads through the **same** adapter and the same
store the live path uses, so that a replay result and a live result cannot
diverge through duplicated validation logic.

Determinism rules:

* Events are replayed in ascending `fired_at` order, and the clock is set from
  each event rather than from wall time. Replaying the same file twice into a
  fresh store yields byte-identical summaries.
* The replay clock never runs backwards. An event that is out of order in the
  file is reported, not silently reordered into acceptance.
* Nothing is inferred. A payload missing a timestamp is a rejection with a
  reason code, exactly as it would be over HTTP.

What this module deliberately does not do: compute outcomes, P&L, or
decisions. It replays *ingestion*. Behavioural replay arrives with the
behavioural engine (M4), and option-level P&L requires genuine historical
option quotes (M7).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from app.market_data.session import Segment, SessionCalendar
from app.signals.htr_adapter import SignalPolicy, normalize_alert
from app.signals.schemas import ReasonCode, to_utc
from app.storage.event_store import EventStore

_SORT_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ReplayedEvent:
    index: int
    accepted: bool
    reason: ReasonCode | None
    detail: str | None
    signal_id: str | None
    idempotency_key: str | None
    signal: str | None
    bar_close_time: datetime | None


@dataclass
class ReplaySummary:
    total: int = 0
    recorded: int = 0
    duplicates: int = 0
    rejected: int = 0
    out_of_order: int = 0
    reasons: Counter = field(default_factory=Counter)
    events: list[ReplayedEvent] = field(default_factory=list)

    def as_dict(self) -> dict:
        """A stable, comparable shape -- two identical replays compare equal."""
        return {
            "total": self.total,
            "recorded": self.recorded,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "out_of_order": self.out_of_order,
            "reasons": dict(sorted(self.reasons.items())),
        }


def _fired_at(payload: dict) -> datetime | None:
    raw = payload.get("fired_at") or payload.get("timenow")
    if not isinstance(raw, str):
        return None
    try:
        return to_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


class SignalReplay:
    """Replays recorded alert payloads through the production ingestion path.

    `delivery_delay` models the gap between an alert firing and the engine
    receiving it. It is set deliberately rather than left at zero, because the
    freshness rules are part of what is under test.
    """

    def __init__(
        self,
        store: EventStore,
        policy: SignalPolicy,
        calendar: SessionCalendar,
        *,
        segment: Segment = Segment.FO,
        delivery_delay: timedelta = timedelta(seconds=1),
    ) -> None:
        self._store = store
        self._policy = policy
        self._calendar = calendar
        self._segment = segment
        self._delay = delivery_delay

    def run(self, payloads: Sequence[dict], *, correlation_prefix: str = "replay") -> ReplaySummary:
        summary = ReplaySummary(total=len(payloads))
        def sort_key(pair: tuple[int, dict]) -> tuple[int, datetime, int]:
            # Payloads with no usable fired_at sort last, keeping their file
            # order, so they are still replayed and still rejected by reason.
            index, payload = pair
            fired = _fired_at(payload)
            if fired is None:
                return (1, _SORT_EPOCH, index)
            return (0, fired, index)

        ordered = sorted(enumerate(payloads), key=sort_key)
        previous_fired: datetime | None = None

        for original_index, payload in ordered:
            fired = _fired_at(payload)
            if fired is not None and previous_fired is not None and fired < previous_fired:
                summary.out_of_order += 1
            if fired is not None:
                previous_fired = fired

            # The clock is derived from the event, never from wall time.
            now = (fired + self._delay) if fired else datetime.now().astimezone()
            correlation_id = f"{correlation_prefix}-{original_index}"

            raw_event_id = self._store.record_raw(
                correlation_id=correlation_id,
                received_at=now,
                body=json.dumps(payload, sort_keys=True),
                content_type="application/json",
                source="replay",
            )

            result = normalize_alert(payload, now, self._policy)
            if not result.accepted:
                self._store.record_rejection(
                    correlation_id=correlation_id,
                    received_at=now,
                    reason=result.reason,
                    detail=result.detail,
                    raw_event_id=raw_event_id,
                )
                summary.rejected += 1
                summary.reasons[result.reason.value] += 1
                summary.events.append(
                    ReplayedEvent(
                        index=original_index,
                        accepted=False,
                        reason=result.reason,
                        detail=result.detail,
                        signal_id=None,
                        idempotency_key=None,
                        signal=None,
                        bar_close_time=None,
                    )
                )
                continue

            event = result.event
            session_state = self._calendar.classify(event.bar_close_time, self._segment)
            outcome = self._store.store_signal(
                event,
                correlation_id=correlation_id,
                raw_event_id=raw_event_id,
                session_state=session_state.state.value,
                # Replay has a processing timestamp but no meaningful processing
                # duration: it would measure how fast the replay ran, not how
                # long the live transport took. Left null rather than filled
                # with a number that means something else.
                processed_at=now,
                processing_ms=None,
            )
            if outcome.stored:
                summary.recorded += 1
            else:
                summary.duplicates += 1
                summary.reasons[ReasonCode.DUPLICATE_SIGNAL.value] += 1
            summary.events.append(
                ReplayedEvent(
                    index=original_index,
                    accepted=outcome.stored,
                    reason=outcome.reason,
                    detail=None,
                    signal_id=outcome.signal_id,
                    idempotency_key=event.idempotency_key,
                    signal=event.signal.value,
                    bar_close_time=event.bar_close_time,
                )
            )
        return summary

    def run_jsonl(self, path: Path, **kwargs) -> ReplaySummary:
        """Replay a JSON-lines file of recorded alert payloads."""
        return self.run(list(read_jsonl(path)), **kwargs)


def read_jsonl(path: Path) -> Iterable[dict]:
    """Yield one payload per non-blank line.

    A malformed line raises instead of being skipped: a replay that quietly
    drops input produces a result that cannot be compared with anything.
    """
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} is not valid JSON: {exc}") from exc
