"""Official warnings, read as CAP — the format the publishers actually issue.

Weather §5 is unusually specific about warnings, and every requirement in it is
a requirement about *not losing* something: preserve official wording and
attribution, keep updates and cancellations under one alert identity, never
average an official warning away because other forecasts look mild, and never
let missing data establish that there are no warnings. None of that survives a
parser that summarises.

So this module normalises **CAP 1.2** (OASIS Common Alerting Protocol), which is
what MeteoAlarm, DWD, the NWS and most national meteorological services publish.
One parser, many publishers: the feed *index* differs per provider and is a few
lines, while the alert payload is the same standard everywhere. Writing a
bespoke parser per country would multiply the place where wording can be lost.

Three properties this file is responsible for:

* **A test or exercise alert is not a warning.** CAP `status` distinguishes
  `Actual` from `Exercise`, `System`, `Test` and `Draft`. Treating a monthly
  test broadcast as a real severe-weather alert is the loudest possible false
  positive, and nothing downstream could tell the difference afterwards.
* **A bbox is a superset of the true area.** `OfficialWarning` carries a
  bounding box, and CAP areas are polygons and circles. Converting to the
  enclosing box can include a point the polygon excludes — the safe direction
  for an alert. The reverse, trimming to fit, would silently drop a warning
  that genuinely applies.
* **Untrusted XML is bounded before it is parsed.** The transport caps the
  response size; this module additionally refuses a document carrying a
  `DOCTYPE`, since entity expansion is the one XML attack a size cap alone does
  not stop.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from xml.etree import ElementTree

from loop.capabilities.weather.adapters.base import (
    AdapterError,
    HttpTransport,
)
from loop.capabilities.weather.warnings import (
    FeedRead,
    OfficialWarning,
    Severity,
    WarningStatus,
)

logger = logging.getLogger(__name__)

CAP_NAMESPACES = (
    "urn:oasis:names:tc:emergency:cap:1.2",
    "urn:oasis:names:tc:emergency:cap:1.1",
)
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"

#: CAP `status` values that describe a real event. Everything else — Exercise,
#: System, Test, Draft — is explicitly not one, and is dropped with a log line
#: rather than delivered.
REAL_STATUSES = frozenset({"actual"})

#: CAP `msgType` → our lifecycle. `Ack` and `Error` are message-handling
#: chatter about another message, not a state of the alert itself.
MESSAGE_TYPES = {
    "alert": WarningStatus.ACTUAL,
    "update": WarningStatus.UPDATE,
    "cancel": WarningStatus.CANCEL,
}

SEVERITIES = {
    "extreme": Severity.EXTREME,
    "severe": Severity.SEVERE,
    "moderate": Severity.MODERATE,
    "minor": Severity.MINOR,
    "unknown": Severity.UNKNOWN,
}

#: One degree of latitude is ~111.32 km everywhere; longitude shrinks with
#: latitude. Used only to turn a CAP circle into an enclosing box.
KM_PER_DEGREE_LATITUDE = 111.32


class UnsafeDocument(AdapterError):
    """The payload was refused before parsing, not parsed and then judged."""


def _guard(payload: bytes, *, source_id: str) -> None:
    """Refuse a document that could expand on parse.

    `xml.etree` is not hardened against entity expansion, and the project has
    no XML-security dependency. A `DOCTYPE` is the only way to declare the
    entities such an attack needs, and no legitimate CAP alert carries one —
    so refusing them costs nothing and closes the hole exactly.
    """
    head = payload[:2048].lstrip()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in payload[:4096]:
        raise UnsafeDocument(source_id,
                            "document declares a DOCTYPE or entities; refused")


def _parse_xml(payload: bytes, *, source_id: str) -> Any:
    _guard(payload, source_id=source_id)
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise AdapterError(source_id, f"malformed XML: {exc}") from exc


def _local(tag: str) -> str:
    """The tag without its namespace — CAP ships in two live namespace versions."""
    return tag.rsplit("}", 1)[-1]


def _find(element: Any, name: str) -> Any:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _findall(element: Any, name: str) -> list[Any]:
    return [child for child in element if _local(child.tag) == name]


def _text(element: Any, name: str, default: str = "") -> str:
    found = _find(element, name)
    return (found.text or "").strip() if found is not None else default


def parse_cap_time(value: str) -> int | None:
    """CAP times are ISO 8601 with a mandatory offset."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.strip()).timestamp())
    except ValueError:
        logger.warning("Unparseable CAP timestamp %r", value)
        return None


def polygon_bbox(polygon: str) -> tuple[float, float, float, float] | None:
    """`lat,lon lat,lon …` → the enclosing box."""
    points: list[tuple[float, float]] = []
    for pair in polygon.split():
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats), min(lons), max(lats), max(lons))


def circle_bbox(circle: str) -> tuple[float, float, float, float] | None:
    """`lat,lon radiusKm` → the enclosing box.

    The longitude span is widened by 1/cos(latitude); near a pole that
    degenerates, so it is clamped to the whole longitude range rather than
    dividing by something arbitrarily close to zero.
    """
    import math

    centre, _, radius_text = circle.strip().partition(" ")
    parts = centre.split(",")
    if len(parts) != 2:
        return None
    try:
        latitude, longitude = float(parts[0]), float(parts[1])
        radius_km = float(radius_text or 0.0)
    except ValueError:
        return None

    delta_lat = radius_km / KM_PER_DEGREE_LATITUDE
    cosine = math.cos(math.radians(latitude))
    if abs(cosine) < 1e-6:
        return (max(-90.0, latitude - delta_lat), -180.0,
                min(90.0, latitude + delta_lat), 180.0)
    delta_lon = delta_lat / abs(cosine)
    return (latitude - delta_lat, longitude - delta_lon,
            latitude + delta_lat, longitude + delta_lon)


def _merge_boxes(boxes: list[tuple[float, float, float, float]]
                 ) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _select_info(alert: Any, language: str) -> Any | None:
    """Pick the info block in the wanted language, else the first published.

    CAP repeats the whole `<info>` block per language. Falling back to the
    first block rather than to nothing matters: an alert whose language we do
    not have is still an alert, and dropping it would be missing data
    masquerading as no warnings.
    """
    blocks = _findall(alert, "info")
    if not blocks:
        return None
    wanted = language.lower()
    for block in blocks:
        if _text(block, "language").lower().startswith(wanted):
            return block
    return blocks[0]


def parse_cap_alert(payload: bytes, *, publisher: str, fetched_at: int,
                    source_url: str = "", language: str = "en") -> OfficialWarning | None:
    """One CAP alert document → one `OfficialWarning`, or None if not real.

    Returns None for an Exercise/Test/System/Draft alert and for a message type
    that describes another message (`Ack`, `Error`) rather than an event.
    """
    root = _parse_xml(payload, source_id=publisher)
    if _local(root.tag) != "alert":
        raise AdapterError(publisher, f"expected a CAP alert, got {_local(root.tag)!r}")

    status = _text(root, "status").lower()
    if status not in REAL_STATUSES:
        logger.info("Discarding CAP alert with status %r from %s", status, publisher)
        return None

    message_type = _text(root, "msgType").lower()
    lifecycle = MESSAGE_TYPES.get(message_type)
    if lifecycle is None:
        logger.info("Ignoring CAP msgType %r from %s", message_type, publisher)
        return None

    identifier = _text(root, "identifier")
    if not identifier:
        raise AdapterError(publisher, "CAP alert has no identifier")

    # `references` are `sender,identifier,sent` triples. The identifier in the
    # middle is what ties an update or cancellation to the alert it revises —
    # which is the whole mechanism keeping one incident under one identity.
    references = tuple(
        part.split(",")[1] for part in _text(root, "references").split()
        if len(part.split(",")) >= 2)

    info = _select_info(root, language)
    if info is None:
        raise AdapterError(publisher, f"CAP alert {identifier} has no info block")

    boxes: list[tuple[float, float, float, float]] = []
    for area in _findall(info, "area"):
        for polygon in _findall(area, "polygon"):
            box = polygon_bbox(polygon.text or "")
            if box:
                boxes.append(box)
        for circle in _findall(area, "circle"):
            box = circle_bbox(circle.text or "")
            if box:
                boxes.append(box)

    original_severity = _text(info, "severity")
    effective = (parse_cap_time(_text(info, "effective"))
                 or parse_cap_time(_text(root, "sent")) or fetched_at)

    return OfficialWarning(
        publisher=_text(root, "sender") or publisher,
        # The alert identity is the *referenced* original where one exists, so
        # an update and its original share it (weather §5).
        provider_alert_id=references[0] if references else identifier,
        message_id=identifier,
        event_type=_text(info, "event"),
        effective_at=effective,
        expires_at=parse_cap_time(_text(info, "expires")),
        onset_at=parse_cap_time(_text(info, "onset")),
        severity=SEVERITIES.get(original_severity.lower(), Severity.UNKNOWN),
        original_severity=original_severity,
        certainty=_text(info, "certainty"),
        urgency=_text(info, "urgency"),
        # Wording is copied, never rephrased: this is the publisher's text and
        # it carries their authority, not ours.
        instruction=_text(info, "instruction"),
        headline=_text(info, "headline"),
        source_url=_text(info, "web") or source_url,
        status=lifecycle,
        references=references,
        geometry=_merge_boxes(boxes),
        fetched_at=fetched_at,
        issued_at=parse_cap_time(_text(root, "sent")),
    )


def parse_atom_index(payload: bytes, *, source_id: str) -> list[str]:
    """Alert document URLs from an Atom feed index."""
    root = _parse_xml(payload, source_id=source_id)
    urls: list[str] = []
    for entry in root.iter():
        if _local(entry.tag) != "link":
            continue
        href = entry.attrib.get("href", "")
        rel = entry.attrib.get("rel", "alternate")
        if href and rel in ("alternate", "related", ""):
            urls.append(href)
    return list(dict.fromkeys(urls))


class CapWarningFeed:
    """One publisher's CAP feed, read through an injected transport.

    Whether the feed is a *complete snapshot* is a property of the publisher's
    documented semantics, not something to infer from a response — an empty
    feed only clears an earlier alert when the publisher says the feed always
    lists everything currently in force (weather §5). It defaults to False, so
    silence never cancels anything by accident.
    """

    def __init__(self, *, publisher: str, index_url: str,
                 transport: HttpTransport | None = None,
                 complete_snapshot: bool = False, language: str = "en",
                 max_alerts: int = 50) -> None:
        self.publisher = publisher
        self.index_url = index_url
        self._transport = transport or HttpTransport()
        self.complete_snapshot = complete_snapshot
        self.language = language
        self.max_alerts = max_alerts

    def read(self, *, latitude: float, longitude: float, now: int) -> FeedRead:
        """Fetch the index and its alerts. Never raises; reports instead.

        A feed that fails must produce an *unusable* read rather than an empty
        one, because `warning_state` treats an empty usable read as "none in
        this checked feed" — which, after an outage, would be an all-clear
        nobody verified.
        """
        del latitude, longitude          # geometry filtering happens downstream
        try:
            index = self._transport.get(self.index_url)
        except AdapterError as exc:
            return FeedRead(publisher=self.publisher, ok=False, fetched_at=now,
                            error=exc.message)
        if index.status_code != 200:
            return FeedRead(publisher=self.publisher, ok=False, fetched_at=now,
                            error=f"HTTP {index.status_code} from the feed index")

        try:
            urls = parse_atom_index(index.body, source_id=self.publisher)
        except AdapterError as exc:
            return FeedRead(publisher=self.publisher, ok=False, fetched_at=now,
                            error=exc.message)

        warnings: list[OfficialWarning] = []
        failures: list[str] = []
        for url in urls[:self.max_alerts]:
            try:
                response = self._transport.get(url)
                if response.status_code != 200:
                    failures.append(f"{url}: HTTP {response.status_code}")
                    continue
                warning = parse_cap_alert(
                    response.body, publisher=self.publisher, fetched_at=now,
                    source_url=url, language=self.language)
            except AdapterError as exc:
                failures.append(f"{url}: {exc.message}")
                continue
            if warning is not None:
                warnings.append(warning)

        truncated = len(urls) > self.max_alerts
        # A read that could not fetch every alert it was told about is
        # *partial*, and a partial read cannot establish an absence.
        partial_reason = ""
        if failures:
            partial_reason = f"{len(failures)} alert document(s) could not be read"
        elif truncated:
            partial_reason = (f"feed listed {len(urls)} alerts; only "
                              f"{self.max_alerts} were read")

        return FeedRead(
            publisher=self.publisher, ok=True, fetched_at=now, warnings=warnings,
            complete_snapshot=self.complete_snapshot and not partial_reason,
            partial_reason=partial_reason,
            error="; ".join(failures[:3]))


#: MeteoAlarm (EUMETNET) publishes CAP for every European member service and
#: documents its feeds publicly with no credentials. The country code goes in
#: the path; the feed is per-country, which is why configuration names one per
#: location rather than one globally.
METEOALARM_FEED = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{country}"

_COUNTRY = re.compile(r"^[a-z]{2}$")


def meteoalarm_feed(country: str, *, transport: HttpTransport | None = None,
                    language: str = "en") -> CapWarningFeed:
    """A MeteoAlarm feed for one ISO 3166-1 alpha-2 country code."""
    code = country.strip().lower()
    if not _COUNTRY.match(code):
        raise AdapterError("meteoalarm",
                           f"{country!r} is not a two-letter country code")
    return CapWarningFeed(
        publisher=f"MeteoAlarm/{code.upper()}",
        index_url=METEOALARM_FEED.format(country=code),
        transport=transport,
        # Not claimed as a complete snapshot: MeteoAlarm aggregates national
        # services, and an absence in the aggregate is not documented to mean
        # a national service has cancelled anything.
        complete_snapshot=False, language=language)
