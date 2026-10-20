"""Select and validate a small ATM / near-ATM option universe.

This is **market-data work, not trading**. It chooses which option contracts to
subscribe to and quote, and judges whether their quotes are fit to act on. It
opens no position, sizes no order, and imports nothing that could. Whether an
accepted HTR signal becomes a paper trade is the risk manager's and executor's
job (M5/M6), which do not yet exist.

Rules it enforces, all of which fail closed:

* **Expired contracts are excluded.** A contract whose expiry is before the
  session date is never selected. Expiry comparison is in IST, the exchange's
  own date.
* **Identifiers are looked up, never constructed.** Strikes, lot sizes, tick
  sizes and expiries come from the instrument master. The ATM strike is chosen
  from the strikes that exist, not computed and assumed to be listed.
* **A contract missing tradable metadata is rejected**, because the adapter
  already refuses to build such an Instrument -- this is a second gate, not a
  substitute for the first.
* **A quote must be genuinely two-sided, fresh, and inside a spread ceiling** to
  be usable. A one-sided, crossed, stale or wide quote yields a reason code, not
  a silent pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from app.market_data.contracts import Instrument, OptionType, Quote
from app.market_data.protocols import DataUnavailable
from app.market_data.session import IST


class OptionReject(StrEnum):
    """Why a contract or its quote is not fit to act on."""

    NO_UNEXPIRED_EXPIRY = "NO_UNEXPIRED_EXPIRY"
    NO_CONTRACTS_FOR_EXPIRY = "NO_CONTRACTS_FOR_EXPIRY"
    NO_STRIKES = "NO_STRIKES"
    MISSING_METADATA = "MISSING_METADATA"
    NO_QUOTE = "NO_QUOTE"
    QUOTE_NOT_TWO_SIDED = "QUOTE_NOT_TWO_SIDED"
    QUOTE_STALE = "QUOTE_STALE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    MOCK_DATA = "MOCK_DATA"


@dataclass(frozen=True)
class OptionSelectionPolicy:
    # How many strikes either side of ATM to include, per option type.
    near_atm_width: int = 2
    # A quote older (or more future-dated) than this is not actionable.
    max_quote_age: timedelta = timedelta(seconds=5)
    # Ceiling on bid/ask spread as a fraction of the mid, e.g. 0.05 == 5%.
    max_spread_fraction: float = 0.05
    # Reject mock-sourced quotes for any real selection decision.
    allow_mock: bool = False


@dataclass(frozen=True)
class QuoteAssessment:
    instrument: Instrument
    quote: Quote | None
    usable: bool
    reasons: tuple[OptionReject, ...] = ()
    spread_fraction: float | None = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.instrument.symbol,
            "strike": self.instrument.strike,
            "option_type": self.instrument.option_type.value
            if self.instrument.option_type
            else None,
            "usable": self.usable,
            "reasons": [reason.value for reason in self.reasons],
            "spread_fraction": self.spread_fraction,
        }


@dataclass(frozen=True)
class OptionUniverse:
    underlying_symbol: str
    expiry: date
    atm_strike: float
    spot: float
    contracts: tuple[Instrument, ...]
    as_of: datetime

    @property
    def strikes(self) -> tuple[float, ...]:
        return tuple(sorted({c.strike for c in self.contracts if c.strike is not None}))


def nearest_unexpired_expiry(
    master, underlying_symbol: str, on: date
) -> date:
    """The soonest listed expiry that has not already passed.

    Raises rather than returning an expired contract: an expired option has no
    live market, and selecting one would be fabricating tradability.
    """
    try:
        expiries = master.option_expiries(underlying_symbol)
    except DataUnavailable as exc:
        raise OptionSelectionError(OptionReject.NO_UNEXPIRED_EXPIRY, str(exc)) from exc
    unexpired = [expiry for expiry in expiries if expiry >= on]
    if not unexpired:
        raise OptionSelectionError(
            OptionReject.NO_UNEXPIRED_EXPIRY,
            f"all {len(expiries)} listed expiries for {underlying_symbol!r} are "
            f"before {on.isoformat()}",
        )
    return min(unexpired)


class OptionSelectionError(RuntimeError):
    def __init__(self, reason: OptionReject, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def select_universe(
    master,
    *,
    underlying_symbol: str,
    spot: float,
    now: datetime,
    policy: OptionSelectionPolicy | None = None,
) -> OptionUniverse:
    """Choose the ATM and near-ATM CE/PE contracts for the nearest live expiry.

    `spot` is the underlying's last price; the ATM strike is the listed strike
    nearest to it. Expired expiries are excluded. No quote is needed here --
    this is contract selection; quote assessment is separate.
    """
    policy = policy or OptionSelectionPolicy()
    if not spot > 0:
        raise OptionSelectionError(OptionReject.NO_STRIKES, f"non-positive spot {spot!r}")
    trade_date = now.astimezone(IST).date()
    expiry = nearest_unexpired_expiry(master, underlying_symbol, trade_date)

    try:
        contracts = master.option_contracts(underlying_symbol, expiry)
    except DataUnavailable as exc:
        raise OptionSelectionError(OptionReject.NO_CONTRACTS_FOR_EXPIRY, str(exc)) from exc

    strikes = sorted({c.strike for c in contracts if c.strike is not None})
    if not strikes:
        raise OptionSelectionError(
            OptionReject.NO_STRIKES, f"no strikes listed for {expiry.isoformat()}"
        )
    atm = min(strikes, key=lambda strike: (abs(strike - spot), strike))
    atm_index = strikes.index(atm)
    low = max(0, atm_index - policy.near_atm_width)
    high = min(len(strikes), atm_index + policy.near_atm_width + 1)
    selected_strikes = set(strikes[low:high])

    selected = tuple(
        contract
        for contract in contracts
        if contract.strike in selected_strikes
        and contract.option_type in (OptionType.CE, OptionType.PE)
    )
    # The adapter already rejects a contract missing lot/tick/strike/expiry, so
    # anything selected here is tradable-shaped. This assertion guards against a
    # provider that is laxer than the Upstox adapter.
    for contract in selected:
        if (
            contract.lot_size is None
            or contract.tick_size is None
            or contract.strike is None
            or contract.expiry is None
        ):
            raise OptionSelectionError(
                OptionReject.MISSING_METADATA,
                f"{contract.symbol!r} is missing tradable metadata",
            )

    return OptionUniverse(
        underlying_symbol=underlying_symbol,
        expiry=expiry,
        atm_strike=atm,
        spot=spot,
        contracts=tuple(
            sorted(selected, key=lambda c: (c.strike or 0.0, c.option_type or ""))
        ),
        as_of=now,
    )


def assess_quote(
    instrument: Instrument,
    quote: Quote | None,
    now: datetime,
    policy: OptionSelectionPolicy | None = None,
) -> QuoteAssessment:
    """Judge whether one contract's quote is fit to act on.

    Every failure is a reason code. A quote can fail several ways at once (stale
    and wide, say), and all are reported.
    """
    policy = policy or OptionSelectionPolicy()
    reasons: list[OptionReject] = []
    spread_fraction: float | None = None

    if quote is None:
        return QuoteAssessment(instrument, None, False, (OptionReject.NO_QUOTE,))

    if quote.provenance is not None and quote.provenance.is_mock and not policy.allow_mock:
        reasons.append(OptionReject.MOCK_DATA)

    if not quote.is_two_sided:
        reasons.append(OptionReject.QUOTE_NOT_TWO_SIDED)
    else:
        spread_fraction = quote.spread_fraction_of_mid
        if spread_fraction is not None and spread_fraction > policy.max_spread_fraction:
            reasons.append(OptionReject.SPREAD_TOO_WIDE)

    if quote.is_stale(now, policy.max_quote_age):
        reasons.append(OptionReject.QUOTE_STALE)

    return QuoteAssessment(
        instrument=instrument,
        quote=quote,
        usable=not reasons,
        reasons=tuple(reasons),
        spread_fraction=spread_fraction,
    )


@dataclass(frozen=True)
class UniverseQuoteReport:
    universe: OptionUniverse
    assessments: tuple[QuoteAssessment, ...]

    @property
    def usable(self) -> tuple[QuoteAssessment, ...]:
        return tuple(a for a in self.assessments if a.usable)

    def as_dict(self) -> dict:
        return {
            "underlying": self.universe.underlying_symbol,
            "expiry": self.universe.expiry.isoformat(),
            "atm_strike": self.universe.atm_strike,
            "spot": self.universe.spot,
            "as_of": self.universe.as_of.isoformat(),
            "contracts": len(self.universe.contracts),
            "usable": len(self.usable),
            "assessments": [a.as_dict() for a in self.assessments],
        }


def assess_universe(
    universe: OptionUniverse,
    quote_source,
    now: datetime,
    policy: OptionSelectionPolicy | None = None,
) -> UniverseQuoteReport:
    """Fetch and assess quotes for every contract in a universe."""
    policy = policy or OptionSelectionPolicy()
    quotes = quote_source.quotes(universe.contracts)
    assessments = tuple(
        assess_quote(contract, quotes.get(contract.symbol), now, policy)
        for contract in universe.contracts
    )
    return UniverseQuoteReport(universe=universe, assessments=assessments)


# `field` is imported for dataclasses that may grow mutable defaults later; keep
# the import meaningful to the linter without an unused-import warning.
_ = field
