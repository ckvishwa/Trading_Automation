"""Feed health: detect silence, staleness, and reconnect without hammering.

An open TCP connection is not a working feed. A provider can hold a socket open
and send nothing, and the dangerous failure is the one where the engine keeps
deciding on a price that stopped updating ten minutes ago. So health is measured
from the last *message*, and the default on uncertainty is to report the feed
unusable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class FeedState(StrEnum):
    NEVER_CONNECTED = "NEVER_CONNECTED"
    HEALTHY = "HEALTHY"
    SILENT = "SILENT"
    DISCONNECTED = "DISCONNECTED"


@dataclass
class FeedWatchdog:
    """Tracks whether a feed is currently trustworthy.

    `silence_limit` should exceed the provider's heartbeat interval; otherwise a
    quiet but healthy feed is reported silent.
    """

    silence_limit: timedelta = timedelta(seconds=30)
    last_message_at: datetime | None = None
    connected: bool = False

    def on_connect(self, moment: datetime) -> None:
        self.connected = True
        self.last_message_at = moment

    def on_message(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FeedWatchdog requires timezone-aware timestamps")
        # Never move the marker backwards: an out-of-order message must not make
        # a stalling feed look fresh.
        if self.last_message_at is None or moment > self.last_message_at:
            self.last_message_at = moment

    def on_disconnect(self) -> None:
        self.connected = False

    def state(self, now: datetime) -> FeedState:
        if self.last_message_at is None:
            return FeedState.NEVER_CONNECTED
        if not self.connected:
            return FeedState.DISCONNECTED
        if now - self.last_message_at > self.silence_limit:
            return FeedState.SILENT
        return FeedState.HEALTHY

    def is_usable(self, now: datetime) -> bool:
        """Fail closed: only an explicitly healthy feed may be acted on."""
        return self.state(now) is FeedState.HEALTHY


@dataclass
class ReconnectBackoff:
    """Exponential backoff with full jitter, capped.

    Jitter matters with a single provider and several subscriptions: without it
    every stream retries in lockstep after an outage and the reconnect storm
    trips the provider's rate limit.
    """

    base_seconds: float = 1.0
    max_seconds: float = 60.0
    multiplier: float = 2.0
    attempt: int = 0
    _random: random.Random = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._random is None:
            self._random = random.Random()

    def next_delay(self) -> float:
        ceiling = min(self.max_seconds, self.base_seconds * self.multiplier**self.attempt)
        self.attempt += 1
        return self._random.uniform(0.0, ceiling)

    def reset(self) -> None:
        self.attempt = 0
