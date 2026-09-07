"""`weather.forecast` and `weather.prepare` as trusted application ports.

The weather capability was complete as a library and unreachable as a
capability: `WeatherService` could select sources, fetch, reconcile and render
a brief, but nothing registered it under an operation name, so a routine step
saying ``capability: weather.prepare`` resolved to nothing and an activated
routine produced no work at all.

These are *trusted ports*, not a pack. A pack ships its own manifest and is
discovered; a trusted port is supplied by the application itself and can only
be named, never invented, by a manifest's ``tools:`` list — the same shape
`vault.search` and `reminder.schedule` already use in the composition root.

Two properties this module is responsible for keeping:

* **No model is required.** `render_brief` is deterministic, and so is
  `advice_text` below. Weather §6 permits a local model to *phrase* advice more
  concisely; it never permits one to decide the advice. A routine that fires at
  07:00 must produce its briefing on a machine with no model configured.
* **Only coordinates, fields and a time range leave the process.** The request
  is built from the routine's own arguments and the configured location table,
  never from the profile, calendar titles or conversation text (weather §6).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from loop.capabilities.runners import RegisteredHandler
from loop.capabilities.weather.adapters.base import AdapterError
from loop.capabilities.weather.adapters.cap import (
    CapWarningFeed,
    meteoalarm_feed,
)
from loop.capabilities.weather.bundle import WeatherBrief, WeatherRequest
from loop.capabilities.weather.service import WeatherService
from loop.core.clock import Clock, SystemClock
from loop.core.errors import InvalidInput
from loop.runtime.authority import AuthorityContext

logger = logging.getLogger(__name__)

#: Arguments a routine step or a pack may pass. Deliberately closed: an
#: unexpected key here is a routine that means something we are not doing, and
#: silently ignoring it would produce advice for the wrong request.
FORECAST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "location_ref": {"type": "string"},
        "latitude": {"type": "number"},
        "longitude": {"type": "number"},
        "timezone": {"type": "string"},
        "horizon_hours": {"type": "number"},
        "fields": {"type": "array", "items": {"type": "string"}},
        "purposes": {"type": "array", "items": {"type": "string"}},
        "departure_time": {"type": "string"},
    },
    "additionalProperties": False,
}


def build_request(arguments: dict[str, Any]) -> WeatherRequest:
    """Turn routine arguments into a typed request, refusing to guess.

    A missing location is *not* filled in from the timezone: `Europe/Berlin` is
    most of a country, and a confident forecast for the wrong city is worse
    than a question (WF16). `WeatherRequest.resolved_location` raises
    `NeedsClarification` when it cannot resolve, which is the honest outcome.
    """
    unresolved = [key for key, value in arguments.items() if value == "$required"]
    if unresolved:
        raise InvalidInput(
            "This routine still needs " + ", ".join(sorted(unresolved))
            + " before it can run.",
            details={"missing": sorted(unresolved)})

    # Only keys the caller actually supplied are passed through, so the
    # request keeps `WeatherRequest`'s own defaults for everything else rather
    # than this module restating them and drifting from them.
    optional: dict[str, Any] = {}
    if arguments.get("fields"):
        optional["fields"] = tuple(str(f) for f in arguments["fields"])
    if arguments.get("purposes"):
        optional["purposes"] = tuple(str(p) for p in arguments["purposes"])
    if arguments.get("horizon_hours") is not None:
        optional["horizon_hours"] = float(arguments["horizon_hours"])

    return WeatherRequest(
        location_ref=str(arguments.get("location_ref", "")),
        latitude=_optional_float(arguments.get("latitude")),
        longitude=_optional_float(arguments.get("longitude")),
        timezone=str(arguments.get("timezone", "")),
        **optional)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def advice_text(brief: WeatherBrief) -> str:
    """One deliverable message, deterministically.

    Uncertainty is included rather than trimmed for brevity: an outage, an
    unconfirmed warning feed or two sources that disagree change what the
    advice is worth, and dropping those lines to make a tidier message is
    exactly how a briefing starts implying an all-clear nobody verified.
    """
    lines = [brief.headline]
    lines.extend(f"- {item}" for item in brief.forecast_summary[1:])
    if brief.preparation:
        lines.append("")
        lines.extend(f"• {item}" for item in brief.preparation)
    if brief.uncertainty:
        lines.append("")
        lines.extend(f"({item})" for item in brief.uncertainty)
    return "\n".join(lines).strip()


def weather_handlers(service: WeatherService, *, clock: Clock | None = None
                     ) -> dict[str, RegisteredHandler]:
    """The two ports, bound to one already-configured `WeatherService`."""
    active = clock or SystemClock()

    def _now() -> int:
        return int(active.now().timestamp())

    def forecast(arguments: dict[str, Any], context: AuthorityContext
                 ) -> dict[str, Any]:
        bundle = service.forecast(build_request(arguments), now=_now(),
                                  privacy=context.privacy)
        return {
            "bundle_id": bundle.id,
            "status": bundle.overall_status.value,
            "checked_at": bundle.generated_at,
            "sources": sorted({sample.source_id for sample in bundle.samples}),
            "unavailable": [error.source_id for error in bundle.source_errors],
        }

    def prepare(arguments: dict[str, Any], context: AuthorityContext
                ) -> dict[str, Any]:
        brief = service.prepare(build_request(arguments), now=_now(),
                                privacy=context.privacy)
        return {
            "answer": advice_text(brief),
            "headline": brief.headline,
            "summary": list(brief.forecast_summary),
            "preparation": list(brief.preparation),
            "uncertainty": list(brief.uncertainty),
            "bundle_id": brief.bundle_id,
            "checked_at": brief.checked_at,
            "sources": list(brief.source_links) or [brief.bundle_id],
        }

    return {
        "weather.forecast": RegisteredHandler(
            forecast, description="Compare forecasts for one location.",
            input_schema=FORECAST_INPUT_SCHEMA, owner_role="daily_life"),
        "weather.prepare": RegisteredHandler(
            prepare, description="Advice for one location and time window.",
            input_schema=FORECAST_INPUT_SCHEMA, owner_role="daily_life"),
    }


def load_locations(vault_root: Path | None, policy_relative: str
                   ) -> dict[str, dict[str, Any]]:
    """Named locations from the vault manifest (interfaces §7).

    Deployment configuration lives in `Settings`; *personal* behaviour —
    including where "home" is — lives in the vault, where the owner can edit it
    without a deploy. That is also why this is not an environment variable: a
    home coordinate is personal data, and the vault is the place the owner
    already controls and backs up.

    An entry without both coordinates is skipped with a warning rather than
    half-loaded. A location resolving to a partial dict would surface much
    later as a confusing type error inside an adapter, while a skipped one
    surfaces immediately as the honest "which location?" question.
    """
    if vault_root is None:
        return {}
    path = vault_root / policy_relative
    if not path.is_file():
        return {}
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        logger.warning("Vault manifest %s is not valid YAML; no named "
                       "locations were loaded", policy_relative)
        return {}
    if not isinstance(manifest, dict):
        return {}

    locations: dict[str, dict[str, Any]] = {}
    for name, entry in (manifest.get("locations") or {}).items():
        if not isinstance(entry, dict) or entry.get("latitude") is None \
                or entry.get("longitude") is None:
            logger.warning("Location %r in the vault manifest has no "
                           "coordinates; skipping it", name)
            continue
        locations[str(name)] = {
            "latitude": float(entry["latitude"]),
            "longitude": float(entry["longitude"]),
            "timezone": str(entry.get("timezone", "")),
            "elevation_m": (None if entry.get("elevation_m") is None
                            else float(entry["elevation_m"])),
        }
    return locations


def load_warning_feeds(vault_root: Path | None, policy_relative: str, *,
                       transport: Any | None = None) -> list[CapWarningFeed]:
    """Official warning feeds declared in the vault manifest (weather §5).

    Configured, never assumed: an unconfigured feed leaves `warning_state` at
    `unknown`, which is the honest answer. Guessing a country from the
    location's coordinates and subscribing to its national feed would make
    Loop assert an all-clear on the strength of a feed nobody chose.

    ```yaml
    warning_feeds:
      - country: de          # MeteoAlarm, per ISO 3166-1 alpha-2 code
        language: en
      - publisher: DWD
        index_url: https://opendata.dwd.de/.../warnings.atom
        complete_snapshot: false
    ```
    """
    if vault_root is None:
        return []
    path = vault_root / policy_relative
    if not path.is_file():
        return []
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        logger.warning("Vault manifest %s is not valid YAML; no warning feeds "
                       "were loaded", policy_relative)
        return []
    if not isinstance(manifest, dict):
        return []

    feeds: list[CapWarningFeed] = []
    for entry in manifest.get("warning_feeds") or []:
        if not isinstance(entry, dict):
            continue
        language = str(entry.get("language", "en"))
        try:
            if entry.get("country"):
                feeds.append(meteoalarm_feed(str(entry["country"]),
                                             transport=transport,
                                             language=language))
            elif entry.get("index_url"):
                feeds.append(CapWarningFeed(
                    publisher=str(entry.get("publisher") or entry["index_url"]),
                    index_url=str(entry["index_url"]), transport=transport,
                    # Only the publisher's own documentation can establish
                    # this, so it is opt-in and defaults to False.
                    complete_snapshot=bool(entry.get("complete_snapshot", False)),
                    language=language))
            else:
                logger.warning("Warning feed entry %r names neither a country "
                               "nor an index_url; skipping it", entry)
        except AdapterError as exc:
            logger.warning("Skipping warning feed %r: %s", entry, exc.message)
    return feeds
