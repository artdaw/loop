"""Cloud spend reservations against a daily ceiling (interfaces §cloud).

Token budgets bound one request; this bounds the *money*, across every request
in a day. They are separate ledgers because they answer different questions and
fail at different times.

The ordering is what makes it work: reserve the estimated **maximum** cost
before the call, settle the actual cost after. Charging after the fact means N
concurrent calls can each check the same remaining balance and each proceed —
the check passes N times and the spend happens N times.

Two refusals are deliberate:

* **Unknown pricing blocks the call.** A model with no pinned price cannot be
  reserved against, and guessing its cost is how a budget becomes decorative.
* **An unsettled reservation stays held.** If a call's outcome is unknown, the
  money may or may not have been spent; releasing the reservation would let
  uncertainty fund further calls (A20).

Neither refusal touches local inference, which costs nothing and stays available.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import text

from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import BudgetExhausted, Unavailable

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class ReservationState(str, Enum):
    HELD = "held"
    SETTLED = "settled"
    RELEASED = "released"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelPrice:
    """A pinned, dated price. Not a claim about current vendor pricing."""

    model_id: str
    input_usd_per_1k: float
    output_usd_per_1k: float
    pinned_on: str

    def estimate(self, *, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1000.0 * self.input_usd_per_1k
                + output_tokens / 1000.0 * self.output_usd_per_1k)


class PriceBook:
    """Prices pinned during cloud adapter setup (interfaces §cloud)."""

    def __init__(self, prices: list[ModelPrice] | None = None) -> None:
        self._by_model = {price.model_id: price for price in prices or []}

    def add(self, price: ModelPrice) -> None:
        self._by_model[price.model_id] = price

    def get(self, model_id: str) -> ModelPrice | None:
        return self._by_model.get(model_id)

    def require(self, model_id: str) -> ModelPrice:
        price = self._by_model.get(model_id)
        if price is None:
            raise Unavailable(
                f"No pinned price for {model_id!r}, so its cost cannot be "
                f"reserved. Pin a dated price before enabling this model.",
                details={"model_id": model_id})
        return price


@dataclass
class Reservation:
    id: str
    model_id: str
    estimated_usd: float
    state: ReservationState = ReservationState.HELD
    actual_usd: float | None = None

    @property
    def held_usd(self) -> float:
        """What this reservation still ties up."""
        if self.state is ReservationState.SETTLED:
            return self.actual_usd or 0.0
        if self.state is ReservationState.RELEASED:
            return 0.0
        # HELD and UNKNOWN both continue to hold the full estimate.
        return self.estimated_usd


class DailySpendLedger:
    """Reservations and settlements against one day's ceiling.

    Thread-safe: without the lock, two concurrent calls can each observe the
    same headroom and both proceed, which is precisely the overspend A20
    describes.
    """

    def __init__(self, *, daily_limit_usd: float, prices: PriceBook,
                 day: str = "", sessions: sessionmaker[Session] | None = None,
                 clock: Clock | None = None) -> None:
        self.daily_limit_usd = daily_limit_usd
        self.prices = prices
        self.day = day
        # Optional: without it the ledger is exactly the in-memory object it
        # always was, which is what every existing unit test still constructs.
        # With it, every reservation and settlement is write-through, so a
        # fresh process for the same day sees what was already committed
        # rather than reopening the full daily budget (A20).
        self._sessions = sessions
        self._clock = clock or SystemClock()
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.Lock()
        if self._sessions is not None:
            self._ensure_table()
            self._load()

    # ------------------------------------------------------------------ #
    # Persistence (optional)
    # ------------------------------------------------------------------ #
    def _ensure_table(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS spend_reservations (
                    id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    estimated_usd REAL NOT NULL,
                    actual_usd REAL,
                    state TEXT NOT NULL,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )"""))
            session.commit()

    def _load(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            rows = session.execute(text(
                "SELECT id, model_id, estimated_usd, actual_usd, state "
                "FROM spend_reservations WHERE day = :day"),
                {"day": self.day}).all()
        for row in rows:
            self._reservations[row[0]] = Reservation(
                id=row[0], model_id=row[1], estimated_usd=row[2],
                actual_usd=row[3], state=ReservationState(row[4]))

    def _persist_new(self, reservation: Reservation) -> None:
        if self._sessions is None:
            return
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO spend_reservations (id, day, model_id, "
                "estimated_usd, actual_usd, state, created_at, updated_at) "
                "VALUES (:id, :day, :model_id, :estimated, :actual, :state, "
                ":now, :now)"),
                {"id": reservation.id, "day": self.day,
                 "model_id": reservation.model_id,
                 "estimated": reservation.estimated_usd,
                 "actual": reservation.actual_usd,
                 "state": reservation.state.value, "now": now})
            session.commit()

    def _persist_update(self, reservation: Reservation) -> None:
        if self._sessions is None:
            return
        now = to_micros(self._clock.now())
        with self._sessions() as session:
            session.execute(text(
                "UPDATE spend_reservations SET actual_usd = :actual, "
                "state = :state, updated_at = :now WHERE id = :id"),
                {"actual": reservation.actual_usd,
                 "state": reservation.state.value, "now": now,
                 "id": reservation.id})
            session.commit()

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #
    @property
    def committed_usd(self) -> float:
        return sum(r.held_usd for r in self._reservations.values())

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.daily_limit_usd - self.committed_usd)

    @property
    def has_unknown_outcomes(self) -> bool:
        return any(r.state is ReservationState.UNKNOWN
                   for r in self._reservations.values())

    def get(self, reservation_id: str) -> Reservation | None:
        return self._reservations.get(reservation_id)

    # ------------------------------------------------------------------ #
    # Reservation
    # ------------------------------------------------------------------ #
    def reserve(self, *, reservation_id: str, model_id: str,
                max_input_tokens: int, max_output_tokens: int) -> Reservation:
        """Hold the estimated maximum cost, or refuse the call."""
        price = self.prices.require(model_id)
        estimate = price.estimate(input_tokens=max_input_tokens,
                                  output_tokens=max_output_tokens)

        with self._lock:
            if reservation_id in self._reservations:
                return self._reservations[reservation_id]

            committed = sum(r.held_usd for r in self._reservations.values())
            if committed + estimate > self.daily_limit_usd:
                raise BudgetExhausted(
                    "This call would exceed the authorised daily cloud budget.",
                    details={"day": self.day, "committed_usd": round(committed, 4),
                             "estimate_usd": round(estimate, 4),
                             "limit_usd": self.daily_limit_usd})

            reservation = Reservation(id=reservation_id, model_id=model_id,
                                      estimated_usd=estimate)
            self._reservations[reservation_id] = reservation
            self._persist_new(reservation)
            return reservation

    def settle(self, reservation_id: str, *, input_tokens: int,
               output_tokens: int) -> Reservation:
        """Record the actual cost, freeing the unused part of the estimate."""
        with self._lock:
            reservation = self._require(reservation_id)
            price = self.prices.require(reservation.model_id)
            reservation.actual_usd = price.estimate(input_tokens=input_tokens,
                                                    output_tokens=output_tokens)
            reservation.state = ReservationState.SETTLED
            self._persist_update(reservation)
            return reservation

    def release(self, reservation_id: str) -> Reservation:
        """Free a reservation for a call that provably never happened."""
        with self._lock:
            reservation = self._require(reservation_id)
            reservation.state = ReservationState.RELEASED
            reservation.actual_usd = 0.0
            self._persist_update(reservation)
            return reservation

    def mark_unknown(self, reservation_id: str) -> Reservation:
        """The call's outcome is unknown; keep holding the full estimate.

        Releasing here would treat "we do not know whether we spent this" as
        "we did not spend it", and let the uncertainty pay for the next call.
        """
        with self._lock:
            reservation = self._require(reservation_id)
            reservation.state = ReservationState.UNKNOWN
            self._persist_update(reservation)
            return reservation

    def _require(self, reservation_id: str) -> Reservation:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise KeyError(f"no reservation {reservation_id!r}")
        return reservation


def cloud_call_permitted(*, cloud_available: bool, local_only: bool,
                         ledger: DailySpendLedger | None) -> tuple[bool, str]:
    """Every condition a cloud call needs, in one place (A19).

    An API key sitting in the environment is not authorisation. Cloud requires
    configuration *and* enablement *and* budget, and private data vetoes all of
    them.
    """
    if local_only:
        return False, "the context is private, so it stays on the local model"
    if not cloud_available:
        return False, ("cloud is not available: it needs CLOUD_ENABLED, an API "
                       "key, a model id and a positive daily budget")
    if ledger is None:
        return False, "no spend ledger is configured for cloud calls"
    if ledger.daily_limit_usd <= 0:
        return False, "the authorised daily cloud budget is zero"
    if ledger.remaining_usd <= 0:
        return False, "the daily cloud budget is exhausted"
    return True, "permitted"
