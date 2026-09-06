"""Costs, currencies and budget comparison (travel §3, §4).

Money is integer minor units and `Decimal`, never binary floating point. This is
not fastidiousness: `0.1 + 0.2` is famously not `0.3`, and a trip total that is
€1,199.9999999 either passes or fails a €1,200 hard limit depending on rounding
nobody chose.

Two refusals:

* **No invented conversion.** Cross-currency totals need a dated, sourced rate.
  Without one the answer is separate totals and *no* budget verdict — an
  estimated rate produces a number that looks like a comparison and is not one.
* **Unknown mandatory costs make compliance tentative.** A total that omits a
  compulsory city tax is not under budget; it is a total of the wrong thing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

logger = logging.getLogger(__name__)

#: Minor units per major unit, by ISO 4217 code. Not every currency uses 100.
MINOR_UNITS = {"EUR": 2, "USD": 2, "GBP": 2, "CHF": 2, "JPY": 0, "ISK": 0}


class Basis(str, Enum):
    PER_PERSON = "per_person"
    PER_ROOM = "per_room"
    PER_GROUP = "per_group"


class Quality(str, Enum):
    QUOTE = "quote"
    ESTIMATE = "estimate"


class BudgetFit(str, Enum):
    WITHIN = "within"
    OVER = "over"
    TENTATIVE = "tentative"
    UNCOMPARABLE = "uncomparable"

    @property
    def is_verdict(self) -> bool:
        """Whether this states budget compliance, as opposed to declining to."""
        return self in (BudgetFit.WITHIN, BudgetFit.OVER)


def minor_units(currency: str) -> int:
    return MINOR_UNITS.get(currency.upper(), 2)


def to_decimal(amount_minor: int, currency: str) -> Decimal:
    """Convert minor units to a decimal major amount, exactly."""
    exponent = minor_units(currency)
    return Decimal(amount_minor).scaleb(-exponent)


def format_money(amount_minor: int, currency: str) -> str:
    exponent = minor_units(currency)
    return f"{to_decimal(amount_minor, currency):.{exponent}f} {currency.upper()}"


@dataclass
class MoneyEstimate:
    """One cost line with its scope, quality and provenance (travel §4)."""

    category: str
    amount_min_minor: int
    amount_max_minor: int
    currency: str
    basis: Basis = Basis.PER_GROUP
    quantity: int = 1
    quality: Quality = Quality.ESTIMATE
    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    fetched_at: int = 0
    expires_at: int | None = None
    evidence_refs: tuple[str, ...] = ()
    #: A named mandatory cost whose amount is not known. Blocks a firm verdict.
    unknown_mandatory: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.amount_max_minor < self.amount_min_minor:
            raise ValueError("amount_max_minor is below amount_min_minor")
        self.currency = self.currency.upper()

    def total_minor(self, *, travelers: int) -> tuple[int, int]:
        """Scale to a group total (travel §3).

        Per-person costs multiply by the traveler count; per-room and per-group
        costs are shared and counted once, times their quantity.
        """
        multiplier = travelers if self.basis is Basis.PER_PERSON else 1
        multiplier *= self.quantity
        return (self.amount_min_minor * multiplier,
                self.amount_max_minor * multiplier)

    def is_fresh(self, *, now: int, ttl_seconds: int) -> bool:
        if self.expires_at is not None:
            return now < self.expires_at
        return now - self.fetched_at <= ttl_seconds


@dataclass(frozen=True)
class ExchangeRate:
    """A dated, sourced rate. There is no default and no fallback."""

    base: str
    quote: str
    rate: Decimal
    as_of: str
    source: str

    def convert_minor(self, amount_minor: int) -> int:
        """Convert minor units, rounding half-up at the target's precision."""
        major = to_decimal(amount_minor, self.base) * self.rate
        exponent = minor_units(self.quote)
        scaled = (major.scaleb(exponent)).quantize(Decimal(1))
        return int(scaled)


@dataclass
class CostTotal:
    """Totals per currency, plus whatever blocks a single figure."""

    by_currency: dict[str, tuple[int, int]] = field(default_factory=dict)
    unknown_mandatory: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def currencies(self) -> list[str]:
        return sorted(self.by_currency)

    @property
    def is_single_currency(self) -> bool:
        return len(self.by_currency) == 1

    def describe(self) -> list[str]:
        return [f"{format_money(low, code)} – {format_money(high, code)}"
                for code, (low, high) in sorted(self.by_currency.items())]


def total_costs(estimates: list[MoneyEstimate], *, travelers: int) -> CostTotal:
    """Sum costs per currency. Currencies are never mixed here (travel §4)."""
    total = CostTotal()
    for estimate in estimates:
        low, high = estimate.total_minor(travelers=travelers)
        current = total.by_currency.get(estimate.currency, (0, 0))
        total.by_currency[estimate.currency] = (current[0] + low,
                                                current[1] + high)
        total.unknown_mandatory.extend(estimate.unknown_mandatory)
    return total


def compare_to_budget(total: CostTotal, *, budget_minor: int, currency: str,
                      hard_limit: bool = True,
                      rate: ExchangeRate | None = None) -> tuple[BudgetFit, str]:
    """Decide budget fit, or decline to (TR08, TR09).

    Declining is a real answer here. "Probably about €1,150" invites a booking
    decision that a separate-totals answer does not.
    """
    currency = currency.upper()
    if not total.by_currency:
        return BudgetFit.TENTATIVE, "no costs have been estimated yet"

    others = [code for code in total.by_currency if code != currency]
    if others:
        if rate is None or not all(
                rate.base == code and rate.quote == currency for code in others):
            return (BudgetFit.UNCOMPARABLE,
                    f"costs are in {', '.join(total.currencies)} and no dated "
                    f"exchange rate is available, so totals are shown separately")
        converted_low = converted_high = 0
        for code, (low, high) in total.by_currency.items():
            if code == currency:
                converted_low += low
                converted_high += high
            else:
                converted_low += rate.convert_minor(low)
                converted_high += rate.convert_minor(high)
        low, high = converted_low, converted_high
    else:
        low, high = total.by_currency[currency]

    if total.unknown_mandatory:
        return (BudgetFit.TENTATIVE,
                f"mandatory costs of unknown amount are not included: "
                f"{', '.join(sorted(set(total.unknown_mandatory)))}")

    if high <= budget_minor:
        return BudgetFit.WITHIN, f"upper estimate {format_money(high, currency)}"
    if low > budget_minor:
        return BudgetFit.OVER, f"lower estimate {format_money(low, currency)}"
    return (BudgetFit.TENTATIVE,
            f"the range {format_money(low, currency)}–"
            f"{format_money(high, currency)} straddles the budget")
