"""ACCEPT / REJECT / ABSTAIN decision interface for HTR signals.

The behavioural engine is a **filter on HTR signals**, never a signal generator.
Its job: given an HTR BUY/SELL and the behavioural features of the bar it fired
on, decide whether the signal qualifies for option selection and risk checks.
An ACCEPT means "qualifies", never "will be traded" and never "will be
profitable".

What this module does NOT do, by design and by the project's rules:

* It does not invent an edge. At M4's start there are no validated historical
  outcome statistics, so the default decision is **REJECT with
  INSUFFICIENT_HISTORICAL_EVIDENCE**. Abstaining — trading nothing — is a valid
  and expected outcome.
* It does not manufacture a win rate, a probability, or an expectancy. A rule
  may carry a configured threshold, but a threshold is explicitly *not* a
  validated statistic, and the decision says so.
* It does not generate BUY/SELL. It only accepts or rejects what HTR produced.

Rules are small, named, pure predicates over the features and the signal. They
are composed; the engine records every reason that fired. Thresholds are
configuration, flagged `validated=False` until out-of-sample testing exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable

from app.behavior.features import BehaviorFeatures, InsufficientHistory, extract_features
from app.market_data.contracts import Candle
from app.signals.schemas import NormalizedSignalEvent, ReasonCode, SignalDirection

DECISION_VERSION = "behavior-v1"


class Decision(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    # ABSTAIN is distinct from REJECT: the engine declined to judge (no
    # evidence, bad data), rather than judging the setup weak.
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class BehaviorDecision:
    signal_id: str | None
    decision: Decision
    reason_codes: tuple[ReasonCode, ...]
    features: BehaviorFeatures | None
    matched_pattern: str | None = None
    sample_count: int = 0
    estimated_edge: None = None  # always None: no validated edge exists yet
    decision_version: str = DECISION_VERSION
    mode: str = "PAPER"
    decided_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def accepted(self) -> bool:
        return self.decision is Decision.ACCEPT

    def as_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "decision": self.decision.value,
            "reason_codes": [code.value for code in self.reason_codes],
            "matched_pattern": self.matched_pattern,
            "sample_count": self.sample_count,
            "estimated_edge": self.estimated_edge,
            "decision_version": self.decision_version,
            "mode": self.mode,
            "feature_version": self.features.feature_version if self.features else None,
        }


# A rule returns a reason code when it objects to the signal, or None when it is
# satisfied. Rules never accept -- they can only withhold acceptance -- so that
# adding a rule can only make the engine more selective.
Rule = Callable[[NormalizedSignalEvent, BehaviorFeatures], ReasonCode | None]


def rule_signal_price_must_be_sane(
    event: NormalizedSignalEvent, features: BehaviorFeatures
) -> ReasonCode | None:
    return None if event.signal_price > 0 else ReasonCode.DATA_INTEGRITY_FAILURE


def rule_direction_must_be_known(
    event: NormalizedSignalEvent, features: BehaviorFeatures
) -> ReasonCode | None:
    return (
        None
        if event.signal in (SignalDirection.BUY, SignalDirection.SELL)
        else ReasonCode.UNKNOWN_SIGNAL_DIRECTION
    )


# The default rule set is deliberately thin: it guards data integrity only. It
# does NOT contain a behavioural threshold that would let a signal through,
# because no such threshold is validated yet.
DEFAULT_RULES: tuple[Rule, ...] = (
    rule_signal_price_must_be_sane,
    rule_direction_must_be_known,
)


@dataclass(frozen=True)
class DecisionConfig:
    """Behavioural thresholds. **None is a validated statistic.**

    These exist so later milestones can wire rules to them once out-of-sample
    testing justifies values. Until `evidence_validated` is True the engine will
    not ACCEPT on behavioural grounds, whatever the thresholds say.
    """

    evidence_validated: bool = False
    min_independent_samples: int = 30
    rules: tuple[Rule, ...] = DEFAULT_RULES


class BehaviorDecisionEngine:
    def __init__(self, config: DecisionConfig | None = None) -> None:
        self._config = config or DecisionConfig()

    def decide(
        self,
        event: NormalizedSignalEvent,
        window: list[Candle] | tuple[Candle, ...],
        *,
        signal_id: str | None = None,
    ) -> BehaviorDecision:
        """Decide on one HTR signal given the candles up to its bar."""
        if window and (
            window[-1].symbol != event.symbol
            or window[-1].timeframe != event.timeframe
            or window[-1].open_time != event.bar_open_time
            or window[-1].close_time != event.bar_close_time
        ):
            return self._reject(signal_id, None, (ReasonCode.DATA_INTEGRITY_FAILURE,))
        try:
            features = extract_features(window)
        except InsufficientHistory:
            return self._reject(
                signal_id, None, (ReasonCode.INSUFFICIENT_HISTORICAL_EVIDENCE,)
            )
        except ValueError:
            return self._reject(signal_id, None, (ReasonCode.DATA_INTEGRITY_FAILURE,))

        return self.decide_from_features(event, features, signal_id=signal_id)

    def decide_from_features(
        self,
        event: NormalizedSignalEvent,
        features: BehaviorFeatures,
        *,
        signal_id: str | None = None,
    ) -> BehaviorDecision:
        """Apply the rules to already-extracted features.

        Split out so a replay can decide from stored features without recomputing
        them, and so tests can drive the rule layer directly.
        """
        if features.decision_bar_time != event.bar_close_time:
            return self._reject(
                signal_id, features, (ReasonCode.DATA_INTEGRITY_FAILURE,)
            )

        objections: list[ReasonCode] = []
        for rule in self._config.rules:
            code = rule(event, features)
            if code is not None and code not in objections:
                objections.append(code)

        if objections:
            return self._reject(signal_id, features, tuple(objections))

        if not self._config.evidence_validated:
            # Data is clean and no rule objected, but there is no validated
            # out-of-sample evidence to justify acting. The honest outcome is to
            # reject for lack of evidence, not to accept by default.
            return self._reject(
                signal_id, features, (ReasonCode.INSUFFICIENT_HISTORICAL_EVIDENCE,)
            )

        return BehaviorDecision(
            signal_id=signal_id,
            decision=Decision.ACCEPT,
            reason_codes=(),
            features=features,
            mode=event.mode,
        )

    def _reject(
        self,
        signal_id: str | None,
        features: BehaviorFeatures | None,
        reasons: tuple[ReasonCode, ...],
    ) -> BehaviorDecision:
        return BehaviorDecision(
            signal_id=signal_id,
            decision=Decision.REJECT,
            reason_codes=reasons,
            features=features,
        )
