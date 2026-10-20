"""Option universe selection and quote assessment. Offline; opens nothing."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.market_data.contracts import OptionType, Provenance, Quote
from app.market_data.mock_provider import RecordedMarketData, mock_provenance, option_instrument
from app.strategy.option_selector import (
    OptionReject,
    OptionSelectionError,
    OptionSelectionPolicy,
    assess_quote,
    assess_universe,
    nearest_unexpired_expiry,
    select_universe,
)

# 10:00 IST on 2026-10-19 == 04:30 UTC.
NOW = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)
UNDERLYING = "NSE:NIFTY"
NEAR = date(2026, 10, 27)
FAR = date(2026, 11, 3)
EXPIRED = date(2026, 10, 6)


def master_with(strikes=range(24000, 25001, 100), expiries=(NEAR, FAR, EXPIRED)):
    provider = RecordedMarketData()
    token = 1
    for expiry in expiries:
        for strike in strikes:
            for option_type in (OptionType.CE, OptionType.PE):
                inst = option_instrument(
                    underlying_symbol=UNDERLYING,
                    expiry=expiry,
                    strike=float(strike),
                    option_type=option_type,
                )
                provider.add_instrument(inst)
                token += 1
    return provider


def quote(bid, ask, *, age_seconds=0.0, mock=False):
    quoted_at = NOW - timedelta(seconds=age_seconds)
    prov = mock_provenance(quoted_at) if mock else Provenance(
        provider="upstox", fetched_at=quoted_at, endpoint="feed", is_mock=False
    )
    return Quote(symbol="x", quoted_at=quoted_at, bid=bid, ask=ask, provenance=prov)


# --- expiry handling -----------------------------------------------------------


def test_the_nearest_unexpired_expiry_skips_past_ones():
    master = master_with()
    assert nearest_unexpired_expiry(master, UNDERLYING, date(2026, 10, 19)) == NEAR


def test_an_expiry_on_today_still_counts_as_live():
    master = master_with(expiries=(date(2026, 10, 19), FAR))
    assert nearest_unexpired_expiry(master, UNDERLYING, date(2026, 10, 19)) == date(
        2026, 10, 19
    )


def test_all_expiries_in_the_past_is_an_error_not_a_stale_pick():
    master = master_with(expiries=(EXPIRED, date(2026, 9, 1)))
    with pytest.raises(OptionSelectionError) as caught:
        nearest_unexpired_expiry(master, UNDERLYING, date(2026, 10, 19))
    assert caught.value.reason is OptionReject.NO_UNEXPIRED_EXPIRY


def test_selection_excludes_expired_contracts():
    master = master_with()
    universe = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW)
    assert universe.expiry == NEAR
    assert all(contract.expiry == NEAR for contract in universe.contracts)
    assert all(contract.expiry >= NOW.date() for contract in universe.contracts)


# --- ATM / near-ATM selection --------------------------------------------------


def test_atm_is_the_listed_strike_nearest_spot():
    master = master_with()
    universe = select_universe(master, underlying_symbol=UNDERLYING, spot=24_473, now=NOW)
    assert universe.atm_strike == 24_500.0  # nearest listed to 24,473


def test_default_width_selects_two_strikes_either_side_both_types():
    master = master_with()
    universe = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW)
    assert universe.strikes == (24_300, 24_400, 24_500, 24_600, 24_700)
    # Each strike has a CE and a PE.
    assert len(universe.contracts) == 10
    types = {(c.strike, c.option_type) for c in universe.contracts}
    assert (24_500.0, OptionType.CE) in types and (24_500.0, OptionType.PE) in types


def test_width_is_configurable():
    master = master_with()
    universe = select_universe(
        master,
        underlying_symbol=UNDERLYING,
        spot=24_500,
        now=NOW,
        policy=OptionSelectionPolicy(near_atm_width=0),
    )
    assert universe.strikes == (24_500,)
    assert len(universe.contracts) == 2


def test_selection_is_clamped_at_the_edge_of_the_chain():
    master = master_with()
    # Spot at the top listed strike: no strikes above it, so the window is
    # one-sided, not padded with invented strikes.
    universe = select_universe(master, underlying_symbol=UNDERLYING, spot=25_000, now=NOW)
    assert universe.atm_strike == 25_000
    assert max(universe.strikes) == 25_000
    assert universe.strikes == (24_800, 24_900, 25_000)


def test_a_non_positive_spot_is_refused():
    master = master_with()
    with pytest.raises(OptionSelectionError) as caught:
        select_universe(master, underlying_symbol=UNDERLYING, spot=0, now=NOW)
    assert caught.value.reason is OptionReject.NO_STRIKES


def test_selection_is_deterministic():
    master = master_with()
    a = select_universe(master, underlying_symbol=UNDERLYING, spot=24_512, now=NOW)
    b = select_universe(master, underlying_symbol=UNDERLYING, spot=24_512, now=NOW)
    assert a.atm_strike == b.atm_strike
    assert [c.symbol for c in a.contracts] == [c.symbol for c in b.contracts]


def test_selection_opens_no_position():
    # The universe is contracts and metadata only -- no quantity, no side, no
    # order. This pins that the returned object carries nothing executable.
    master = master_with()
    universe = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW)
    payload = str(universe.__dict__)
    for forbidden in ("quantity", "transaction_type", "order", "buy", "sell"):
        assert forbidden not in payload.lower()


# --- quote assessment ----------------------------------------------------------


def test_a_healthy_quote_is_usable():
    master = master_with()
    contract = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW).contracts[0]
    assessment = assess_quote(contract, quote(120.0, 120.5), NOW)
    assert assessment.usable
    assert assessment.reasons == ()
    assert assessment.spread_fraction == pytest.approx(0.5 / 120.25)


def test_a_missing_quote_is_rejected():
    master = master_with()
    contract = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW).contracts[0]
    assessment = assess_quote(contract, None, NOW)
    assert not assessment.usable
    assert assessment.reasons == (OptionReject.NO_QUOTE,)


def test_a_one_sided_quote_is_rejected():
    master = master_with()
    contract = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW).contracts[0]
    assessment = assess_quote(contract, quote(None, 120.5), NOW)
    assert OptionReject.QUOTE_NOT_TWO_SIDED in assessment.reasons


def test_a_wide_spread_is_rejected():
    master = master_with()
    contract = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW).contracts[0]
    # 10 wide on a ~100 mid == 10%, beyond the 5% default.
    assessment = assess_quote(contract, quote(95.0, 105.0), NOW)
    assert OptionReject.SPREAD_TOO_WIDE in assessment.reasons
    assert not assessment.usable


def test_a_stale_quote_is_rejected():
    master = master_with()
    contract = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW).contracts[0]
    assessment = assess_quote(contract, quote(120.0, 120.5, age_seconds=30), NOW)
    assert OptionReject.QUOTE_STALE in assessment.reasons


def test_multiple_quote_faults_are_all_reported():
    master = master_with()
    contract = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW).contracts[0]
    assessment = assess_quote(contract, quote(90.0, 110.0, age_seconds=30), NOW)
    assert OptionReject.SPREAD_TOO_WIDE in assessment.reasons
    assert OptionReject.QUOTE_STALE in assessment.reasons


def test_mock_quotes_are_rejected_for_a_real_decision():
    master = master_with()
    contract = select_universe(master, underlying_symbol=UNDERLYING, spot=24_500, now=NOW).contracts[0]
    assessment = assess_quote(contract, quote(120.0, 120.5, mock=True), NOW)
    assert OptionReject.MOCK_DATA in assessment.reasons
    # Explicitly allowing mock (for a fixture-driven test) clears it.
    allowed = assess_quote(
        contract, quote(120.0, 120.5, mock=True), NOW, OptionSelectionPolicy(allow_mock=True)
    )
    assert allowed.usable


def test_assess_universe_reports_per_contract():
    master = master_with()
    universe = select_universe(
        master,
        underlying_symbol=UNDERLYING,
        spot=24_500,
        now=NOW,
        policy=OptionSelectionPolicy(near_atm_width=0),
    )
    provider = RecordedMarketData()
    for contract in universe.contracts:
        provider.add_quote(
            Quote(
                symbol=contract.symbol,
                quoted_at=NOW,
                bid=120.0,
                ask=120.4,
                provenance=Provenance(
                    provider="upstox", fetched_at=NOW, endpoint="feed", is_mock=False
                ),
            )
        )
    report = assess_universe(universe, provider, NOW)
    assert len(report.assessments) == 2
    assert len(report.usable) == 2
    assert report.as_dict()["usable"] == 2
