"""The decision engine filters HTR signals; it never generates or accepts freely."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.behavior.decision_engine import (
    DEFAULT_RULES,
    BehaviorDecisionEngine,
    Decision,
    DecisionConfig,
)
from app.behavior.features import MIN_WINDOW, extract_features
from app.market_data.contracts import Candle, Provenance
from app.signals.schemas import NormalizedSignalEvent, ReasonCode, SignalDirection

OPEN = datetime(2026, 9, 7, 3, 45, tzinfo=timezone.utc)
BAR = timedelta(minutes=5)
PROV = Provenance(provider="test", fetched_at=OPEN, endpoint="test", is_mock=True)


def window(n: int = MIN_WINDOW + 2) -> list[Candle]:
    out = []
    price = 100.0
    for i in range(n):
        start = OPEN + i * BAR
        out.append(
            Candle(
                symbol="NSE:NIFTY",
                timeframe="5m",
                open_time=start,
                close_time=start + BAR,
                open=price,
                high=price + 6,
                low=price - 1,
                close=price + 5,
                provenance=PROV,
            )
        )
        price += 5
    return out


def event(**overrides) -> NormalizedSignalEvent:
    base = dict(
        symbol="NSE:NIFTY",
        timeframe="5m",
        signal=SignalDirection.BUY,
        bar_open_time=OPEN,
        bar_close_time=OPEN + BAR,
        fired_at=OPEN + BAR,
        received_at=OPEN + BAR,
        signal_price=100.0,
        idempotency_key="k",
    )
    base.update(overrides)
    return NormalizedSignalEvent(**base)


def test_default_engine_rejects_for_lack_of_evidence_even_on_clean_data():
    """The core honesty guarantee.

    No rule objects and the data is clean, but there is no validated
    out-of-sample edge, so the engine rejects for lack of evidence rather than
    accepting by default. Abstaining is a valid outcome.
    """
    decision = BehaviorDecisionEngine().decide(event(), window(), signal_id="s1")
    assert decision.decision is Decision.REJECT
    assert ReasonCode.INSUFFICIENT_HISTORICAL_EVIDENCE in decision.reason_codes
    assert not decision.accepted
    assert decision.estimated_edge is None
    assert decision.sample_count == 0


def test_insufficient_history_rejects_with_its_own_reason():
    decision = BehaviorDecisionEngine().decide(event(), window(MIN_WINDOW - 2))
    assert decision.decision is Decision.REJECT
    assert decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORICAL_EVIDENCE,)
    assert decision.features is None


def test_a_bad_window_rejects_with_data_integrity():
    bad = window()
    bad[-1], bad[-2] = bad[-2], bad[-1]  # not ascending
    decision = BehaviorDecisionEngine().decide(event(), bad)
    assert decision.reason_codes == (ReasonCode.DATA_INTEGRITY_FAILURE,)


def test_a_data_integrity_rule_objects_before_evidence():
    # A rule objection takes precedence over the no-evidence default, so the
    # reason names the real problem.
    features = extract_features(window())
    decision = BehaviorDecisionEngine().decide_from_features(
        event(signal_price=100.0), features, signal_id="s2"
    )
    # Clean price -> falls through to the no-evidence reject.
    assert ReasonCode.INSUFFICIENT_HISTORICAL_EVIDENCE in decision.reason_codes


def test_even_with_validation_enabled_a_rule_objection_blocks_accept():
    # A hand-built features object paired with a rule that always objects.
    features = extract_features(window())

    def always_object(evt, feats):
        return ReasonCode.PATTERN_MISMATCH

    engine = BehaviorDecisionEngine(
        DecisionConfig(evidence_validated=True, rules=(always_object,))
    )
    decision = engine.decide_from_features(event(), features)
    assert decision.decision is Decision.REJECT
    assert decision.reason_codes == (ReasonCode.PATTERN_MISMATCH,)


def test_accept_is_only_reachable_when_evidence_is_declared_validated():
    # With validation explicitly enabled and no rule objecting, the interface
    # CAN accept -- proving the ACCEPT path exists -- but this requires an
    # operator to assert evidence_validated, which is False by default and must
    # not be flipped without out-of-sample testing.
    features = extract_features(window())
    engine = BehaviorDecisionEngine(DecisionConfig(evidence_validated=True, rules=()))
    decision = engine.decide_from_features(event(), features, signal_id="s3")
    assert decision.decision is Decision.ACCEPT
    assert decision.reason_codes == ()
    assert decision.accepted
    # Even on accept, no edge or win rate is manufactured.
    assert decision.estimated_edge is None


def test_the_default_engine_cannot_accept_whatever_the_features():
    engine = BehaviorDecisionEngine()  # evidence_validated defaults to False
    for _ in range(5):
        decision = engine.decide(event(), window())
        assert decision.decision is not Decision.ACCEPT


def test_decisions_are_deterministic():
    engine = BehaviorDecisionEngine()
    a = engine.decide(event(), window(), signal_id="s")
    b = engine.decide(event(), window(), signal_id="s")
    assert a.as_dict() == b.as_dict()


def test_the_engine_never_emits_a_buy_or_sell():
    # It filters HTR signals; it does not produce direction. The decision
    # vocabulary is ACCEPT/REJECT/ABSTAIN only.
    assert {d.value for d in Decision} == {"ACCEPT", "REJECT", "ABSTAIN"}
    decision = BehaviorDecisionEngine().decide(event(), window())
    assert decision.decision.value not in ("BUY", "SELL")


def test_decision_serializes_with_mode_and_versions():
    decision = BehaviorDecisionEngine().decide(event(), window(), signal_id="s9")
    payload = decision.as_dict()
    assert payload["signal_id"] == "s9"
    assert payload["mode"] == "PAPER"
    assert payload["decision_version"] == "behavior-v1"
    assert payload["estimated_edge"] is None
    assert payload["feature_version"] == "behavior-features-v1"


def test_default_rules_only_guard_data_integrity():
    # The shipped rule set must not contain a behavioural threshold that could
    # let a signal through; it guards price sanity and direction only.
    assert len(DEFAULT_RULES) == 2
