"""Official warnings read as CAP (weather §5, M6).

The payloads below are shaped like real CAP 1.2 alerts — the format MeteoAlarm,
DWD and the NWS all publish — rather than a convenient simplification. Nothing
here touches a network: the transport is injected, exactly as the forecast
adapters do it.

Most of these tests are about what must *not* happen. A monthly test broadcast
must not become a severe-weather alert; a feed outage must not read as an
all-clear; an update must not become a second unrelated warning; and an alert
in a language we did not ask for must not disappear.
"""

from __future__ import annotations

import datetime as dt

import pytest

from loop.capabilities.weather.adapters.base import (
    AdapterError,
    HttpResponse,
    HttpTransport,
)
from loop.capabilities.weather.adapters.cap import (
    CapWarningFeed,
    UnsafeDocument,
    circle_bbox,
    meteoalarm_feed,
    parse_atom_index,
    parse_cap_alert,
    polygon_bbox,
)
from loop.capabilities.weather.warnings import (
    FeedRead,
    Severity,
    WarningState,
    WarningStatus,
    apply_lifecycle,
    warning_state,
)

#: 2026-09-06T09:00Z, inside every alert window below. Derived rather than
#: written as a literal so the constant and the CAP timestamps cannot drift.
NOW = int(dt.datetime.fromisoformat("2026-09-06T09:00:00+00:00").timestamp())
BERLIN = (52.52, 13.405)


def cap_alert(*, identifier: str = "2.49.0.1.276.0.DWD.20260906090000.1",
              status: str = "Actual", msg_type: str = "Alert",
              severity: str = "Severe", references: str = "",
              expires: str = "2026-09-06T18:00:00+00:00",
              event: str = "SEVERE THUNDERSTORM",
              instruction: str = "Secure loose objects and avoid open spaces.",
              extra_info: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>{identifier}</identifier>
  <sender>opendata@dwd.de</sender>
  <sent>2026-09-06T09:00:00+00:00</sent>
  <status>{status}</status>
  <msgType>{msg_type}</msgType>
  <scope>Public</scope>
  <references>{references}</references>
  <info>
    <language>en-GB</language>
    <category>Met</category>
    <event>{event}</event>
    <urgency>Immediate</urgency>
    <severity>{severity}</severity>
    <certainty>Likely</certainty>
    <effective>2026-09-06T08:00:00+00:00</effective>
    <onset>2026-09-06T10:00:00+00:00</onset>
    <expires>{expires}</expires>
    <headline>Official warning of SEVERE THUNDERSTORM</headline>
    <instruction>{instruction}</instruction>
    <web>https://www.wettergefahren.de/</web>
    <area>
      <areaDesc>Berlin</areaDesc>
      <polygon>52.3,13.1 52.7,13.1 52.7,13.8 52.3,13.8 52.3,13.1</polygon>
    </area>
  </info>{extra_info}
</alert>""".encode()


ATOM_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>MeteoAlarm</title>
  <entry>
    <title>Berlin thunderstorm</title>
    <link rel="alternate" href="https://feeds.example.invalid/alerts/one.xml"/>
  </entry>
  <entry>
    <title>Brandenburg wind</title>
    <link rel="alternate" href="https://feeds.example.invalid/alerts/two.xml"/>
  </entry>
</feed>"""


class FakeTransport(HttpTransport):
    """Serves a URL→payload map and records what was asked for."""

    def __init__(self, responses: dict[str, bytes | int]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, *, params=None) -> HttpResponse:
        self.calls.append(url)
        payload = self.responses.get(url)
        if payload is None:
            raise AdapterError(url, "no route in this fake")
        if isinstance(payload, int):
            return HttpResponse(payload, b"")
        return HttpResponse(200, payload)


# --------------------------------------------------------------------------- #
# Parsing one alert
# --------------------------------------------------------------------------- #
def test_a_cap_alert_keeps_the_publishers_own_wording_and_metadata():
    warning = parse_cap_alert(cap_alert(), publisher="MeteoAlarm/DE",
                              fetched_at=NOW)

    assert warning is not None
    assert warning.publisher == "opendata@dwd.de"
    assert warning.event_type == "SEVERE THUNDERSTORM"
    assert warning.headline == "Official warning of SEVERE THUNDERSTORM"
    assert warning.instruction == "Secure loose objects and avoid open spaces."
    assert warning.severity is Severity.SEVERE
    assert warning.original_severity == "Severe"     # verbatim, not normalised away
    assert warning.urgency == "Immediate"
    assert warning.certainty == "Likely"
    assert warning.source_url == "https://www.wettergefahren.de/"
    assert warning.status is WarningStatus.ACTUAL


def test_cap_times_become_the_windows_the_alert_actually_declares():
    warning = parse_cap_alert(cap_alert(), publisher="p", fetched_at=NOW)

    def at(stamp: str) -> int:
        return int(dt.datetime.fromisoformat(stamp).timestamp())

    assert warning.effective_at == at("2026-09-06T08:00:00+00:00")
    assert warning.onset_at == at("2026-09-06T10:00:00+00:00")
    assert warning.expires_at == at("2026-09-06T18:00:00+00:00")
    assert warning.issued_at == at("2026-09-06T09:00:00+00:00")   # sent
    assert warning.is_valid_at(NOW)


@pytest.mark.parametrize("status", ["Test", "Exercise", "System", "Draft"])
def test_a_test_or_exercise_alert_is_not_a_warning(status):
    """The loudest possible false positive, and undetectable downstream."""
    assert parse_cap_alert(cap_alert(status=status), publisher="p",
                           fetched_at=NOW) is None


@pytest.mark.parametrize("msg_type", ["Ack", "Error"])
def test_message_handling_chatter_is_not_a_warning(msg_type):
    assert parse_cap_alert(cap_alert(msg_type=msg_type), publisher="p",
                           fetched_at=NOW) is None


def test_an_update_keeps_the_original_alerts_identity():
    """Otherwise one storm becomes two unrelated warnings (weather §5)."""
    original = parse_cap_alert(cap_alert(), publisher="p", fetched_at=NOW)
    update = parse_cap_alert(
        cap_alert(identifier="…DWD.20260906120000.2", msg_type="Update",
                  severity="Extreme",
                  references=f"opendata@dwd.de,{original.message_id},"
                             "2026-09-06T09:00:00+00:00"),
        publisher="p", fetched_at=NOW)

    assert update.status is WarningStatus.UPDATE
    assert update.identity == original.identity
    assert update.message_id != original.message_id
    assert update.severity is Severity.EXTREME


def test_a_cancellation_ends_the_alert_it_references():
    original = parse_cap_alert(cap_alert(), publisher="p", fetched_at=NOW)
    cancel = parse_cap_alert(
        cap_alert(identifier="…DWD.3", msg_type="Cancel",
                  references=f"opendata@dwd.de,{original.message_id},"
                             "2026-09-06T09:00:00+00:00"),
        publisher="p", fetched_at=NOW)

    assert cancel.status is WarningStatus.CANCEL
    assert cancel.identity == original.identity

    known = apply_lifecycle(
        {}, FeedRead(publisher="p", ok=True, fetched_at=NOW,
                     warnings=[original]), now=NOW)
    assert original.identity in known

    known = apply_lifecycle(
        known, FeedRead(publisher="p", ok=True, fetched_at=NOW,
                        warnings=[cancel]), now=NOW)

    # `apply_lifecycle` drops a cancelled alert outright: the identity is what
    # ties the cancellation to it, and losing that link is what would leave a
    # withdrawn warning being reported as active.
    assert original.identity not in known


def test_an_unknown_severity_is_unknown_rather_than_downgraded():
    warning = parse_cap_alert(cap_alert(severity="Catastrophic"),
                              publisher="p", fetched_at=NOW)

    assert warning.severity is Severity.UNKNOWN
    assert warning.original_severity == "Catastrophic"


def test_an_alert_in_another_language_is_still_an_alert():
    """Dropping it would be missing data pretending to be no warning."""
    german_only = cap_alert().replace(b"<language>en-GB</language>",
                                      b"<language>de-DE</language>")

    warning = parse_cap_alert(german_only, publisher="p", fetched_at=NOW,
                              language="en")

    assert warning is not None
    assert warning.event_type == "SEVERE THUNDERSTORM"


def test_the_requested_language_wins_when_the_alert_offers_several():
    second = """
  <info>
    <language>de-DE</language>
    <event>SCHWERES GEWITTER</event>
    <urgency>Immediate</urgency>
    <severity>Severe</severity>
    <certainty>Likely</certainty>
    <effective>2026-09-06T08:00:00+00:00</effective>
    <expires>2026-09-06T18:00:00+00:00</expires>
    <headline>Amtliche WARNUNG vor SCHWEREM GEWITTER</headline>
  </info>"""
    payload = cap_alert(extra_info=second)

    english = parse_cap_alert(payload, publisher="p", fetched_at=NOW, language="en")
    german = parse_cap_alert(payload, publisher="p", fetched_at=NOW, language="de")

    assert english.event_type == "SEVERE THUNDERSTORM"
    assert german.event_type == "SCHWERES GEWITTER"


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def test_a_polygon_becomes_the_box_that_encloses_it():
    warning = parse_cap_alert(cap_alert(), publisher="p", fetched_at=NOW)

    assert warning.geometry == (52.3, 13.1, 52.7, 13.8)
    assert warning.covers_point(*BERLIN)


def test_a_circle_becomes_a_box_that_contains_it():
    box = circle_bbox("52.52,13.405 25.0")

    assert box is not None
    min_lat, min_lon, max_lat, max_lon = box
    assert min_lat < 52.52 < max_lat
    assert min_lon < 13.405 < max_lon
    # ~25 km north-south is ~0.22°, and the box must not be smaller.
    assert (max_lat - min_lat) >= 0.44


def test_an_unparseable_area_yields_no_geometry_rather_than_a_wrong_one():
    assert polygon_bbox("not coordinates") is None
    assert circle_bbox("nonsense") is None


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
def test_a_document_declaring_entities_is_refused_before_parsing():
    """`xml.etree` is not hardened against entity expansion; a size cap is not
    enough on its own, and no legitimate CAP alert carries a DOCTYPE."""
    bomb = (b'<?xml version="1.0"?>\n<!DOCTYPE alert [<!ENTITY a "aaaaaaaaaa">]>'
            b"\n<alert><identifier>x</identifier></alert>")

    with pytest.raises(UnsafeDocument):
        parse_cap_alert(bomb, publisher="p", fetched_at=NOW)


def test_malformed_xml_is_a_named_adapter_error():
    with pytest.raises(AdapterError):
        parse_cap_alert(b"<alert><unclosed>", publisher="p", fetched_at=NOW)


def test_a_document_that_is_not_an_alert_is_refused():
    with pytest.raises(AdapterError):
        parse_cap_alert(b"<feed xmlns='http://www.w3.org/2005/Atom'/>",
                        publisher="p", fetched_at=NOW)


# --------------------------------------------------------------------------- #
# Reading a whole feed
# --------------------------------------------------------------------------- #
def test_the_index_lists_the_alert_documents_to_fetch():
    assert parse_atom_index(ATOM_INDEX, source_id="p") == [
        "https://feeds.example.invalid/alerts/one.xml",
        "https://feeds.example.invalid/alerts/two.xml",
    ]


def _feed(responses: dict) -> tuple[CapWarningFeed, FakeTransport]:
    transport = FakeTransport(responses)
    return CapWarningFeed(publisher="MeteoAlarm/DE",
                          index_url="https://feeds.example.invalid/de",
                          transport=transport), transport


def test_a_feed_read_returns_the_alerts_it_fetched():
    feed, transport = _feed({
        "https://feeds.example.invalid/de": ATOM_INDEX,
        "https://feeds.example.invalid/alerts/one.xml": cap_alert(),
        "https://feeds.example.invalid/alerts/two.xml": cap_alert(
            identifier="second", event="GALE"),
    })

    read = feed.read(latitude=BERLIN[0], longitude=BERLIN[1], now=NOW)

    assert read.ok and read.usable
    assert [w.event_type for w in read.warnings] == ["SEVERE THUNDERSTORM", "GALE"]
    assert len(transport.calls) == 3


def test_a_failed_index_is_an_unusable_read_not_an_empty_one():
    """An empty *usable* read means "none in this checked feed" — an all-clear."""
    feed, _ = _feed({})          # the index itself is not routed

    read = feed.read(latitude=BERLIN[0], longitude=BERLIN[1], now=NOW)

    assert read.ok is False
    assert read.usable is False
    assert read.warnings == []


def test_an_alert_that_cannot_be_fetched_makes_the_read_partial():
    feed, _ = _feed({
        "https://feeds.example.invalid/de": ATOM_INDEX,
        "https://feeds.example.invalid/alerts/one.xml": cap_alert(),
        "https://feeds.example.invalid/alerts/two.xml": 503,
    })

    read = feed.read(latitude=BERLIN[0], longitude=BERLIN[1], now=NOW)

    assert read.ok is True
    assert read.usable is False          # partial: cannot establish an absence
    assert read.partial_reason
    assert len(read.warnings) == 1


def test_a_truncated_feed_is_partial_rather_than_quietly_shortened():
    index = ATOM_INDEX
    feed = CapWarningFeed(publisher="p", index_url="i",
                          transport=FakeTransport({
                              "i": index,
                              "https://feeds.example.invalid/alerts/one.xml":
                                  cap_alert(),
                              "https://feeds.example.invalid/alerts/two.xml":
                                  cap_alert(identifier="second")}),
                          max_alerts=1)

    read = feed.read(latitude=BERLIN[0], longitude=BERLIN[1], now=NOW)

    assert read.usable is False
    assert "only 1" in read.partial_reason


def test_an_empty_feed_never_claims_a_complete_snapshot_unless_documented():
    empty = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>"""
    feed, _ = _feed({"https://feeds.example.invalid/de": empty})

    read = feed.read(latitude=BERLIN[0], longitude=BERLIN[1], now=NOW)

    assert read.ok and read.usable
    assert read.warnings == []
    assert read.complete_snapshot is False


# --------------------------------------------------------------------------- #
# What the bundle is then allowed to say
# --------------------------------------------------------------------------- #
def test_an_active_alert_over_the_location_is_reported_active():
    feed, _ = _feed({
        "https://feeds.example.invalid/de": ATOM_INDEX,
        "https://feeds.example.invalid/alerts/one.xml": cap_alert(),
        "https://feeds.example.invalid/alerts/two.xml": cap_alert(
            identifier="second", event="GALE"),
    })
    read = feed.read(latitude=BERLIN[0], longitude=BERLIN[1], now=NOW)

    state, active, caveats = warning_state(
        [read], now=NOW, latitude=BERLIN[0], longitude=BERLIN[1],
        window=(NOW, NOW + 3 * 3600))

    assert state is WarningState.ACTIVE
    assert active
    del caveats


def test_an_outage_leaves_the_warning_state_unknown_not_clear():
    feed, _ = _feed({})
    read = feed.read(latitude=BERLIN[0], longitude=BERLIN[1], now=NOW)

    state, active, caveats = warning_state(
        [read], now=NOW, latitude=BERLIN[0], longitude=BERLIN[1],
        window=(NOW, NOW + 3 * 3600))

    assert state is WarningState.UNKNOWN
    assert active == []
    assert caveats


# --------------------------------------------------------------------------- #
# MeteoAlarm wiring
# --------------------------------------------------------------------------- #
def test_a_meteoalarm_feed_is_built_per_country():
    feed = meteoalarm_feed("de")

    assert feed.publisher == "MeteoAlarm/DE"
    assert feed.index_url.endswith("-de")
    # MeteoAlarm aggregates national services; an absence there is not
    # documented to cancel a national alert.
    assert feed.complete_snapshot is False


def test_a_country_code_that_is_not_one_is_refused():
    with pytest.raises(AdapterError):
        meteoalarm_feed("Germany")
