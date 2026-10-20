"""Verification-state lattice and candle parity. Offline and deterministic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.market_data.contracts import Candle, Provenance
from app.market_data.verification import (
    FeedVerification,
    VerificationState,
    compare_candles,
)

OPEN = datetime(2026, 9, 7, 3, 45, tzinfo=timezone.utc)
BAR = timedelta(minutes=5)
PROV = Provenance(provider="test", fetched_at=OPEN, endpoint="test", is_mock=True)


def candle(index: int, o: float, h: float, low: float, c: float) -> Candle:
    start = OPEN + index * BAR
    return Candle(
        symbol="NSE:NIFTY",
        timeframe="5m",
        open_time=start,
        close_time=start + BAR,
        open=o,
        high=h,
        low=low,
        close=c,
        provenance=PROV,
    )


# --- state lattice -------------------------------------------------------------


def test_no_credential_is_blocked():
    verification = FeedVerification(has_credential=False)
    assert verification.state() is VerificationState.AUTHENTICATION_BLOCKED
    assert not verification.verified


def test_a_rejected_credential_is_failed_not_blocked():
    verification = FeedVerification(has_credential=True, authenticated=False)
    assert verification.state() is VerificationState.AUTHENTICATION_FAILED
    assert not verification.verified


def test_a_credential_that_never_opened_a_socket_is_not_connected():
    verification = FeedVerification(
        has_credential=True, authenticated=None, socket_connected=False
    )
    assert verification.state() is VerificationState.AUTHENTICATION_FAILED


def test_an_open_socket_with_no_ticks_is_only_connected():
    verification = FeedVerification(
        has_credential=True, authenticated=True, socket_connected=True
    )
    assert verification.state() is VerificationState.SOCKET_CONNECTED
    assert not verification.verified


def test_a_connected_socket_on_a_closed_market_reports_market_closed():
    verification = FeedVerification(
        has_credential=True,
        authenticated=True,
        socket_connected=True,
        market_open=False,
        valid_ticks=0,
    )
    assert verification.state() is VerificationState.MARKET_CLOSED


def test_invalid_ticks_with_no_valid_ones_is_live_data_invalid():
    verification = FeedVerification(
        has_credential=True,
        authenticated=True,
        socket_connected=True,
        market_open=True,
        invalid_ticks=5,
        valid_ticks=0,
    )
    assert verification.state() is VerificationState.LIVE_DATA_INVALID
    assert not verification.verified


def test_valid_ticks_without_a_completed_candle_are_not_yet_verified():
    # Ticks arriving is not the same as a bar completing.
    verification = FeedVerification(
        has_credential=True,
        authenticated=True,
        socket_connected=True,
        market_open=True,
        valid_ticks=3,
        completed_candles=0,
    )
    assert verification.state() is VerificationState.SOCKET_CONNECTED


def test_valid_ticks_and_a_completed_candle_reach_live_stream_verified():
    verification = FeedVerification(
        has_credential=True,
        authenticated=True,
        socket_connected=True,
        market_open=True,
        valid_ticks=50,
        completed_candles=1,
    )
    assert verification.state() is VerificationState.LIVE_STREAM_VERIFIED
    assert verification.verified


def test_parity_is_the_strongest_state():
    verification = FeedVerification(
        has_credential=True,
        authenticated=True,
        socket_connected=True,
        market_open=True,
        valid_ticks=50,
        completed_candles=3,
        parity=compare_candles([candle(0, 1, 2, 0.5, 1.5)], [candle(0, 1, 2, 0.5, 1.5)]),
    )
    assert verification.state() is VerificationState.HISTORICAL_PARITY_VERIFIED
    assert verification.verified


def test_a_connected_socket_is_never_automatically_verified():
    # The core guarantee: connection alone proves nothing about the data.
    verification = FeedVerification(
        has_credential=True, authenticated=True, socket_connected=True
    )
    assert not verification.verified


# --- candle parity -------------------------------------------------------------


def test_identical_candles_have_parity():
    built = [candle(0, 100, 110, 95, 108), candle(1, 108, 112, 106, 109)]
    report = compare_candles(built, built)
    assert report.parity
    assert report.compared == 2 and report.matched == 2


def test_an_ohlc_mismatch_is_reported_per_field():
    built = [candle(0, 100, 110, 95, 108)]
    historical = [candle(0, 100, 111, 95, 107)]  # high and close differ
    report = compare_candles(built, historical)
    assert not report.parity
    fields = {mismatch.field for mismatch in report.mismatches}
    assert fields == {"high", "close"}
    assert report.matched == 0


def test_a_bar_missing_from_the_built_set_is_reported_not_filled():
    built = [candle(0, 100, 110, 95, 108)]
    historical = [candle(0, 100, 110, 95, 108), candle(1, 108, 112, 106, 109)]
    report = compare_candles(built, historical)
    assert report.missing_in_built == (OPEN + BAR,)
    assert not report.parity
    # The missing bar is reported, never interpolated into the built set.
    assert len(built) == 1


def test_an_extra_built_bar_is_reported():
    built = [candle(0, 100, 110, 95, 108), candle(1, 108, 112, 106, 109)]
    historical = [candle(0, 100, 110, 95, 108)]
    report = compare_candles(built, historical)
    assert report.extra_in_built == (OPEN + BAR,)
    assert not report.parity


def test_an_empty_comparison_is_never_parity():
    assert not compare_candles([], []).parity
    assert not compare_candles([candle(0, 1, 2, 0.5, 1.5)], []).parity


def test_a_tolerance_can_absorb_provider_rounding_but_defaults_to_zero():
    built = [candle(0, 100.0, 110.0, 95.0, 108.02)]
    historical = [candle(0, 100.0, 110.0, 95.0, 108.0)]
    assert not compare_candles(built, historical).parity
    assert compare_candles(built, historical, price_tolerance=0.05).parity


def test_the_report_serializes_to_a_stable_shape():
    built = [candle(0, 100, 110, 95, 108)]
    historical = [candle(0, 100, 111, 95, 108)]
    payload = compare_candles(built, historical).as_dict()
    assert payload["parity"] is False
    assert payload["mismatches"][0]["field"] == "high"
    assert payload["mismatches"][0]["built"] == 110
    assert payload["mismatches"][0]["historical"] == 111
