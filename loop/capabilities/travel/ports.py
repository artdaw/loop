"""`travel.brief` as a trusted application port (EX14).

Travel needs a port for the same reason weather does: EX14 requires it to load
as an ordinary pack, sharing the registry, lifecycle and executor with every
other capability. A pack in adapter mode names a port the host already
supplies, so enabling it grants no capability the application had not already
decided to offer.

What this exposes is deliberately the *safe* half of travel: validate a brief
and say what is still missing. Research and monitoring cost money and need
provider authority, so they are not reachable by enabling a pack.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from loop.capabilities.runners import RegisteredHandler
from loop.capabilities.travel.brief import (
    DateWindow,
    PlaceRef,
    Travelers,
    TripBrief,
    can_claim_feasibility,
    external_query_payload,
    validate_brief,
)
from loop.runtime.authority import AuthorityContext

#: `place`, not `destination`: the latter is a RESERVED argument naming a
#: delivery target, and `ToolWrapper` strips it so untrusted content cannot
#: redirect where output goes. A pack reusing the word would silently lose it.
BRIEF_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "place": {"type": "string"},
        "origin": {"type": "string"},
        "start_date": {"type": "string"},
        "end_date": {"type": "string"},
        "adults": {"type": "integer"},
        "interests": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def _date(value: Any) -> dt.date | None:
    """Parse an ISO date, or None. A malformed date is not a date."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def travel_handlers() -> dict[str, RegisteredHandler]:
    def brief(arguments: dict[str, Any], context: AuthorityContext
              ) -> dict[str, Any]:
        del context
        window = DateWindow(
            start_date=_date(arguments.get("start_date")),
            end_date=_date(arguments.get("end_date")))
        destinations = ((PlaceRef(label=str(arguments["place"])),)
                        if arguments.get("place") else ())
        trip = TripBrief(
            destinations=destinations,
            origin=(PlaceRef(label=str(arguments["origin"]))
                    if arguments.get("origin") else None),
            date_window=window,
            travelers=Travelers(adults=int(arguments.get("adults", 1))),
            # Weighted interests; an unweighted list is treated as equal
            # weight rather than silently dropped.
            interests={str(i): 1.0 for i in arguments.get("interests", ())})

        missing = validate_brief(trip)
        blocking = [m for m in missing if m.blocking]
        questions = [m.question for m in missing]

        # Two different things, deliberately not collapsed into one "ready".
        # A *blocking* gap is an ambiguity — "which Lisbon?" — that makes
        # planning impossible. A missing origin or date is not blocking: a
        # tentative outline is still useful, it just cannot claim the trip is
        # feasible. Reporting one flag would lose exactly that distinction,
        # and an empty brief would look healthier than a nearly complete one.
        answer = ("I can plan from this."
                  if not blocking else
                  "I need one thing cleared up before I can plan.")
        if not can_claim_feasibility(trip):
            answer += (" I cannot confirm the trip is feasible without a "
                       "resolved origin and dates.")
        return {
            "answer": " ".join([answer, *questions]).strip(),
            "can_plan": not blocking,
            "can_claim_feasibility": can_claim_feasibility(trip),
            "blocking": [m.field for m in blocking],
            "questions": questions,
            # Exactly the fields an external search may receive (TR21), so a
            # caller can see what would leave before anything does.
            "external_payload": external_query_payload(trip),
            "sources": [],
        }

    return {
        "travel.brief": RegisteredHandler(
            brief, description="Validate a trip brief and name what is missing.",
            input_schema=BRIEF_INPUT_SCHEMA, owner_role="daily_life"),
    }
