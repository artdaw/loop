"""M5: an activated routine actually produces weather advice, after a restart.

This is the milestone's own exit criterion — *"activated routine → weather
comparison → advice → outbox after restart"* — and every step of it runs the
production path. The only fakes are the two things that would otherwise reach a
network: the HTTP transport under the Open-Meteo adapters, and the notification
transport under the outbox. Source selection, fetching, normalization,
comparison, brief rendering, trigger firing, job claiming, notification policy
and outbox delivery are all the real implementations.

The restart is what makes it a *service* rather than a script:
`test_a_separate_operating_system_process_completes_the_routine` fires the
trigger in this process, exits nothing but leaves a row in `jobs`, and then
runs a genuinely separate Python interpreter which finds that row, runs the
routine and delivers. Nothing in memory bridges the two processes.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from loop.app import build_application
from loop.capabilities.weather.adapters.base import HttpResponse, HttpTransport
from loop.capabilities.weather.service import WeatherService
from loop.core.clock import UTC, FrozenClock
from loop.core.settings import Settings
from loop.runtime.notify_policy import Decision
from loop.runtime.outbox import FakeTransport
from loop.runtime.routine_dispatch import (
    ROUTINE_SUBJECT,
    RoutineDispatcher,
)
from loop.runtime.routines import parse_routine

#: 2026-09-06 05:00 UTC is 07:00 in Berlin — the routine's own wall time.
FIRES_AT = dt.datetime(2026, 9, 6, 5, 0, tzinfo=UTC)

BERLIN = {"latitude": 52.52, "longitude": 13.405, "timezone": "Europe/Berlin",
          "elevation_m": 34.0}

MORNING_WEATHER = """---
schema_version: 1
id: morning-weather
title: Morning weather
trigger:
  kind: local_schedule
  days: [mon, tue, wed, thu, fri, sat, sun]
  at: "07:00"
  timezone: Europe/Berlin
steps:
  - capability: weather.prepare
    arguments:
      location_ref: home
      horizon_hours: 3
notification:
  mode: each_occurrence
  destination: "4242"
---

"Every morning give me the weather" — each_occurrence delivery (interfaces §6).
"""


def hourly_payload(now: int, *, probabilities: tuple[int, ...] = (0, 55, 70, 70)
                   ) -> dict:
    """An Open-Meteo response shaped exactly like the recorded live one."""
    times = [dt.datetime.fromtimestamp(now + index * 3600, UTC)
             .strftime("%Y-%m-%dT%H:00") for index in range(len(probabilities))]
    return {
        "latitude": 52.52, "longitude": 13.41, "elevation": 38.0,
        "utc_offset_seconds": 0, "timezone": "GMT",
        "hourly_units": {"time": "iso8601", "temperature_2m": "°C",
                         "precipitation_probability": "%",
                         "wind_speed_10m": "km/h", "precipitation": "mm"},
        "hourly": {
            "time": times,
            "temperature_2m": [12.0, 12.5, 13.0, 13.2][:len(probabilities)],
            "precipitation_probability": list(probabilities),
            "wind_speed_10m": [9.0, 10.0, 11.0, 12.0][:len(probabilities)],
            "precipitation": [0.0, 0.2, 0.6, 0.4][:len(probabilities)],
        },
    }


class RecordedTransport(HttpTransport):
    """Serves one recorded payload and remembers every request made."""

    def __init__(self, payload: dict | None = None, *, status: int = 200) -> None:
        super().__init__()
        self.payload = payload
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, params: dict | None = None) -> HttpResponse:
        self.calls.append((url, dict(params or {})))
        body = json.dumps(self.payload if self.payload is not None
                          else {"error": True, "reason": "provider outage"})
        return HttpResponse(self.status, body.encode("utf-8"))


@pytest.fixture
def weather_transport() -> RecordedTransport:
    return RecordedTransport(hourly_payload(int(FIRES_AT.timestamp())))


def weather_service(transport: HttpTransport) -> WeatherService:
    return WeatherService(transport=transport, locations={"home": BERLIN})


@pytest.fixture
def sends() -> FakeTransport:
    return FakeTransport()


def make_app(settings: Settings, clock: FrozenClock,
             transport: HttpTransport, sends: FakeTransport | None = None):
    return build_application(
        settings, clock=clock, weather=weather_service(transport),
        transports={"telegram": sends} if sends is not None else None)


def activate_morning_weather(app) -> None:
    routine, problems = parse_routine(
        MORNING_WEATHER, known_capabilities=app.known_capabilities())
    assert problems == [], problems
    assert routine is not None
    assert routine.missing == []          # weather.prepare is a real port now
    app.routines.save(routine)
    app.routine_scheduler.activate("morning-weather",
                                   activation_event_id="evt-activation")


# --------------------------------------------------------------------------- #
# Activation writes a schedule
# --------------------------------------------------------------------------- #
def test_activating_a_routine_writes_its_schedule(settings, clock,
                                                  weather_transport):
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)

    triggers = app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather")
    assert len(triggers) == 1
    assert triggers[0].kind == "local_schedule"
    assert triggers[0].definition["at"] == "07:00"
    # 09:00 Berlin on the 5th is past 07:00, so the next firing is the 6th.
    assert triggers[0].next_fire_at == int(FIRES_AT.timestamp() * 1_000_000)


def test_activating_twice_does_not_create_a_second_schedule(settings, clock,
                                                            weather_transport):
    """Two triggers would deliver the same briefing twice every morning."""
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    app.routine_scheduler.ensure_scheduled(app.routines.get("morning-weather"))

    assert len(app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather")) == 1


def test_pausing_a_routine_stops_its_trigger(settings, clock, weather_transport):
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    app.routine_scheduler.pause("morning-weather")

    assert app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather") == []
    # The row survives, so its firing history still blocks a replay.
    assert app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather",
                                    enabled_only=False) != []


def test_reconcile_schedules_an_active_routine_whose_trigger_was_lost(
        settings, clock, weather_transport):
    """A crash between activating and scheduling is otherwise invisible."""
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    for trigger in app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather"):
        app.triggers.disable(trigger.id)

    report = app.routine_scheduler.reconcile()

    assert report.scheduled == ["morning-weather"]
    assert len(app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather")) == 1


# --------------------------------------------------------------------------- #
# Firing queues durable work, and calls no model
# --------------------------------------------------------------------------- #
def test_a_fired_trigger_queues_one_durable_job_without_calling_a_model(
        settings, clock, weather_transport):
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    clock.set(FIRES_AT)

    report = app.service.tick()

    assert report.triggers_fired == 1
    assert app.service.pending_summary()["jobs"] == 1
    # The sweep runs on a timer whether or not anything is due (D16/D17): it
    # must reach neither a model nor a weather provider.
    assert app.model_gateway.audit.cloud_calls == 0
    assert app.model_gateway.audit.local_calls == 0
    assert weather_transport.calls == []


def test_the_same_occurrence_cannot_queue_two_jobs(settings, clock,
                                                   weather_transport):
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    clock.set(FIRES_AT)

    app.service.tick()
    app.service.tick()

    assert app.service.pending_summary()["jobs"] == 1


def test_a_paused_routine_that_still_fires_queues_nothing(settings, clock,
                                                          weather_transport):
    """Belt and braces: the dispatcher rechecks activation at firing time."""
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    app.routines.pause("morning-weather")     # trigger deliberately left enabled
    clock.set(FIRES_AT)

    app.service.tick()

    assert app.service.pending_summary()["jobs"] == 0


# --------------------------------------------------------------------------- #
# A second object graph runs the queued work
# --------------------------------------------------------------------------- #
def test_a_restarted_application_produces_sourced_compared_advice(
        settings, clock, weather_transport, sends):
    """Everything after the trigger is done by a second, unrelated graph."""
    first = make_app(settings, clock, weather_transport)
    activate_morning_weather(first)
    clock.set(FIRES_AT)
    first.service.tick()

    # A completely separate object graph: new engine, new sessionmaker, new
    # stores, new registry, new policy manager. Only the database file is shared.
    restart_clock = FrozenClock(FIRES_AT)
    second_transport = RecordedTransport(hourly_payload(int(FIRES_AT.timestamp())))
    second = make_app(settings, restart_clock, second_transport, sends)

    outcome = second.routine_worker.run_one()

    assert outcome is not None
    assert outcome.state == "succeeded"
    assert outcome.decision == Decision.SEND.value
    # Three independent Open-Meteo models were actually fetched and compared.
    assert len({url for url, _ in second_transport.calls}) == 3
    assert "precipitation_probability" in outcome.message
    assert any("waterproof" in line or "umbrella" in line
               for line in outcome.message.splitlines())
    # Disagreement is explained, never averaged into a consensus nobody has.
    assert "disagree" in outcome.message.lower()


def test_only_coordinates_fields_and_a_window_leave_the_process(
        settings, clock, weather_transport, sends):
    """weather §6: no profile, address text, calendar titles or conversation."""
    app = make_app(settings, clock, weather_transport, sends)
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()
    app.routine_worker.run_one()

    assert weather_transport.calls
    for _url, params in weather_transport.calls:
        assert set(params) == {"latitude", "longitude", "hourly", "timezone",
                               "start_hour", "end_hour"}


def test_the_advice_reaches_the_outbox_and_is_delivered_once(
        settings, clock, weather_transport, sends):
    app = make_app(settings, clock, weather_transport, sends)
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()
    app.routine_worker.run_one()

    report = app.service.tick()

    assert report.notifications_sent == 1
    assert len(sends.sent) == 1
    delivered = sends.sent[0]
    assert delivered["payload"]["routine"] == "morning-weather"
    assert "precipitation_probability" in delivered["payload"]["text"]

    # A second sweep must not send it again.
    assert app.service.tick().notifications_sent == 0
    assert len(sends.sent) == 1


def test_a_provider_outage_is_reported_as_unavailable_not_as_calm(
        settings, clock, sends):
    """An outage is not a forecast of dry weather (weather §6)."""
    app = make_app(settings, clock, RecordedTransport(None, status=503), sends)
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()

    outcome = app.routine_worker.run_one()

    assert outcome is not None
    assert outcome.state == "succeeded"
    assert "could not get weather data" in outcome.message
    assert "not a forecast of calm weather" in outcome.message


def test_a_routine_without_a_destination_delivers_nothing(
        settings, clock, weather_transport, sends):
    """Guessing a destination for a briefing is a disclosure, not a fallback."""
    document = MORNING_WEATHER.replace('  destination: "4242"\n', "")
    blank = settings.model_copy(update={"telegram_chat_id": ""})
    app = make_app(blank, clock, weather_transport, sends)
    routine, problems = parse_routine(document,
                                      known_capabilities=app.known_capabilities())
    assert problems == []
    app.routines.save(routine)
    app.routine_scheduler.activate("morning-weather",
                                   activation_event_id="evt-activation")
    clock.set(FIRES_AT)
    app.service.tick()

    outcome = app.routine_worker.run_one()

    assert outcome is not None
    assert outcome.decision == Decision.SUPPRESS.value
    assert "no destination" in outcome.reason
    assert outcome.message                      # the advice itself was produced
    assert sends.sent == []


def test_a_routine_paused_between_firing_and_claiming_sends_nothing(
        settings, clock, weather_transport, sends):
    app = make_app(settings, clock, weather_transport, sends)
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()
    app.routines.pause("morning-weather")

    outcome = app.routine_worker.run_one()

    assert outcome is not None
    assert outcome.decision == Decision.SUPPRESS.value
    assert app.service.tick().notifications_sent == 0
    assert sends.sent == []


# --------------------------------------------------------------------------- #
# A genuinely separate operating-system process
# --------------------------------------------------------------------------- #
RESTART_SCRIPT = textwrap.dedent('''
    """Run the queued routine job. Nothing is shared with the parent but the
    database file — this interpreter builds its own object graph from it."""
    import datetime as dt, json, sys

    from loop.app import build_application
    from loop.capabilities.weather.adapters.base import HttpResponse, HttpTransport
    from loop.capabilities.weather.service import WeatherService
    from loop.core.clock import UTC, FrozenClock
    from loop.core.settings import Settings
    from loop.runtime.outbox import FakeTransport

    payload = json.loads(sys.argv[2])
    fires_at = dt.datetime.fromtimestamp(float(sys.argv[3]), UTC)

    class Transport(HttpTransport):
        def get(self, url, *, params=None):
            return HttpResponse(200, json.dumps(payload).encode("utf-8"))

    settings = Settings(_env_file=None, environment="test",
                        timezone="Europe/Berlin", database_url=sys.argv[1],
                        data_dir=sys.argv[4], obsidian_vault_path="",
                        telegram_chat_id="4242", telegram_user_id="99")
    sends = FakeTransport()
    app = build_application(
        settings, clock=FrozenClock(fires_at),
        weather=WeatherService(transport=Transport(),
                               locations={"home": {"latitude": 52.52,
                                                   "longitude": 13.405,
                                                   "timezone": "Europe/Berlin",
                                                   "elevation_m": 34.0}}),
        transports={"telegram": sends})

    outcome = app.routine_worker.run_one()
    ticked = app.service.tick()
    print(json.dumps({
        "claimed": outcome is not None,
        "decision": getattr(outcome, "decision", ""),
        "message": getattr(outcome, "message", ""),
        "delivered": ticked.notifications_sent,
        "sent": [item["payload"]["routine"] for item in sends.sent],
    }))
''')


def test_a_separate_operating_system_process_completes_the_routine(
        settings, clock, weather_transport, tmp_path):
    """The trigger fires here; a different interpreter delivers the briefing.

    Reconstructing Python objects in one process is useful but does not prove
    a restart (plan §4). This kills nothing and starts nothing in-process: the
    child finds its work in `jobs`, builds its own services, and delivers.
    """
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()
    assert app.service.pending_summary()["jobs"] == 1
    # A process that is going away releases its leader lease. Without this the
    # child would correctly refuse to sweep — one leader at a time — and the
    # test would be measuring the lease, not the restart.
    app.service.leader.release()

    script = tmp_path / "restart_worker.py"
    script.write_text(RESTART_SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script), settings.database_url,
         json.dumps(hourly_payload(int(FIRES_AT.timestamp()))),
         str(FIRES_AT.timestamp()), settings.data_dir],
        capture_output=True, text=True, cwd=Path.cwd(),
        env={**os.environ, "PYTHONPATH": str(Path.cwd())}, timeout=180)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["claimed"] is True
    assert result["decision"] == Decision.SEND.value
    assert "precipitation_probability" in result["message"]
    assert result["delivered"] == 1
    assert result["sent"] == ["morning-weather"]

    # And this process agrees: the work is done, nothing is left queued.
    assert app.service.pending_summary()["jobs"] == 0
    assert app.service.pending_summary()["outbox"] == 0


# --------------------------------------------------------------------------- #
# Notification policy governs the delivery, not the routine
# --------------------------------------------------------------------------- #
LATE_ALERT = """---
schema_version: 1
id: late-rain-check
title: Late rain check
trigger:
  kind: local_schedule
  days: [mon, tue, wed, thu, fri, sat, sun]
  at: "23:00"
  timezone: Europe/Berlin
steps:
  - capability: weather.prepare
    arguments:
      location_ref: home
notification:
  destination: "4242"
---

No `mode`, so this is discretionary: the owner chose the subscription, not
the hour it happens to fire at (WF20).
"""

#: 2026-09-05 21:00 UTC is 23:00 in Berlin — inside quiet hours.
QUIET_FIRE = dt.datetime(2026, 9, 5, 21, 0, tzinfo=UTC)


def test_a_discretionary_routine_in_quiet_hours_waits_for_the_digest(
        settings, clock, sends):
    app = make_app(settings, clock,
                   RecordedTransport(hourly_payload(int(QUIET_FIRE.timestamp()))),
                   sends)
    routine, problems = parse_routine(LATE_ALERT,
                                      known_capabilities=app.known_capabilities())
    assert problems == []
    app.routines.save(routine)
    app.routine_scheduler.activate("late-rain-check",
                                   activation_event_id="evt-activation")
    clock.set(QUIET_FIRE)
    app.service.tick()

    outcome = app.routine_worker.run_one()

    assert outcome is not None
    assert outcome.decision == Decision.DEFER_TO_DIGEST.value
    assert "quiet hours" in outcome.reason
    # Queued, but not deliverable yet: the digest hour is still in the future.
    assert app.service.tick().notifications_sent == 0
    assert sends.sent == []

    clock.set(dt.datetime(2026, 9, 6, 6, 30, tzinfo=UTC))   # 08:30 Berlin
    assert app.service.tick().notifications_sent == 1
    assert len(sends.sent) == 1


# --------------------------------------------------------------------------- #
# The port itself: what it refuses to guess
# --------------------------------------------------------------------------- #
def test_the_port_refuses_an_unanswered_required_argument():
    """Reachable from a pack or `do`, not only from an activated routine.

    `RoutineService.activate` already refuses a routine with unmet
    requirements, so this guard is defence in depth — but the port is called
    by more than routines, and `$required` reaching an adapter as a literal
    latitude is how a placeholder becomes a request for somewhere real.
    """
    from loop.capabilities.weather.ports import build_request
    from loop.core.errors import InvalidInput

    with pytest.raises(InvalidInput) as raised:
        build_request({"location_ref": "$required"})
    assert "location_ref" in str(raised.value)


def test_an_unknown_location_asks_rather_than_guessing(settings, clock):
    """A timezone is not a coordinate (WF16)."""
    from loop.core.errors import NeedsClarification

    app = make_app(settings, clock, RecordedTransport(hourly_payload(0)))
    context_owner = app.invoker.handlers["weather.prepare"]
    assert context_owner.owner_role == "daily_life"

    service = weather_service(RecordedTransport(hourly_payload(0)))
    service.locations = {}
    from loop.capabilities.weather.bundle import WeatherRequest
    with pytest.raises(NeedsClarification):
        service.forecast(WeatherRequest(location_ref="home"),
                         now=int(FIRES_AT.timestamp()))


# --------------------------------------------------------------------------- #
# Gaps found by mutation, closed here
# --------------------------------------------------------------------------- #
EVENING_WEATHER = MORNING_WEATHER.replace(
    "id: morning-weather", "id: evening-weather").replace(
    "title: Morning weather", "title: Evening weather").replace(
    'at: "07:00"', 'at: "18:00"')


def test_two_occurrences_of_one_routine_are_two_separate_messages(
        settings, clock, weather_transport, sends):
    """Consecutive days must not collide on one occurrence identity.

    The outbox is keyed on `(subject_ref, occurrence_key)`. If the dispatcher
    passed anything constant per routine — the trigger id, say — the *second*
    morning's briefing would be silently absorbed as a duplicate of the first
    and the owner would simply stop receiving it, with no error anywhere.
    """
    app = make_app(settings, clock, weather_transport, sends)
    activate_morning_weather(app)

    for day in range(2):
        clock.set(FIRES_AT + dt.timedelta(days=day))
        app.service.tick()
        assert app.routine_worker.run_one() is not None
        app.service.tick()

    assert len(sends.sent) == 2
    keys = {item["dedupe_key"] for item in sends.sent}
    assert len(keys) == 2, "both mornings shared one occurrence identity"


def test_the_dispatcher_queues_one_job_per_occurrence(settings, clock,
                                                      weather_transport):
    """The dispatcher's own contract, independent of the firing claim.

    `TriggerService.claim_occurrence` already stops a second sweep dispatching
    the same occurrence, so this is defence in depth — and it is only defence
    if the job's dedupe key actually carries the occurrence, which nothing
    else here would reveal.
    """
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    trigger = app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather")[0]
    dispatcher = RoutineDispatcher(jobs=app.jobs, routines=app.routines)

    first = dispatcher(trigger, "fire", "2026-09-06T07:00+Europe/Berlin")
    second = dispatcher(trigger, "fire", "2026-09-06T07:00+Europe/Berlin")
    third = dispatcher(trigger, "fire", "2026-09-07T07:00+Europe/Berlin")

    assert first is not None
    assert second is None, "the same occurrence queued a second job"
    assert third is not None, "a different occurrence was absorbed as a duplicate"


def test_pausing_one_routine_leaves_another_running(settings, clock,
                                                    weather_transport):
    app = make_app(settings, clock, weather_transport)
    activate_morning_weather(app)
    evening, problems = parse_routine(
        EVENING_WEATHER, known_capabilities=app.known_capabilities())
    assert problems == []
    app.routines.save(evening)
    app.routine_scheduler.activate("evening-weather",
                                   activation_event_id="evt-evening")

    app.routine_scheduler.pause("morning-weather")

    assert app.triggers.for_subject(ROUTINE_SUBJECT, "morning-weather") == []
    still_running = app.triggers.for_subject(ROUTINE_SUBJECT, "evening-weather")
    assert len(still_running) == 1
    assert still_running[0].definition["at"] == "18:00"


COMMUTE_ROUTINE = """---
schema_version: 1
id: commute-rain
title: Commute rain check
trigger:
  kind: local_schedule
  days: [mon, tue, wed, thu, fri]
  at: "07:00"
  timezone: Europe/Berlin
steps:
  - capability: weather.prepare
    arguments:
      location_ref: home
limits:
  scope: commute
notification:
  destination: "4242"
---

Scoped to the commute, so "working from home today" can suppress exactly this
one for the day without touching anything else (interfaces §6).
"""


def test_a_suppressed_scope_stops_delivery_without_stopping_the_routine(
        settings, clock, weather_transport, sends):
    app = make_app(settings, clock, weather_transport, sends)
    routine, problems = parse_routine(COMMUTE_ROUTINE,
                                      known_capabilities=app.known_capabilities())
    assert problems == []
    app.routines.save(routine)
    app.routine_scheduler.activate("commute-rain",
                                   activation_event_id="evt-commute")
    app.notifications.suppress_scope(
        "commute", until=dt.datetime(2026, 9, 7, 22, 0, tzinfo=UTC))
    # Weekdays only, and 2026-09-06 is a Sunday: this routine's next
    # occurrence is Monday morning, not tomorrow.
    clock.set(dt.datetime(2026, 9, 7, 5, 0, tzinfo=UTC))
    app.service.tick()

    outcome = app.routine_worker.run_one()

    assert outcome is not None
    assert outcome.decision == Decision.SUPPRESS.value
    assert "commute" in outcome.reason
    assert app.service.tick().notifications_sent == 0
    assert sends.sent == []
    # Suppressed for the day, not cancelled: the routine is still active.
    assert app.routines.get("commute-rain").is_active


def test_an_undeliverable_briefing_is_reported_not_silently_dropped(
        settings, clock, weather_transport):
    """No transport for the channel is a stated outcome, not "nothing was due".

    Found by running the shipped command against the real API with no channel
    configured: the sweep reported "0 notification(s) sent", which is exactly
    what an idle service also reports — while a briefing had in fact been
    produced and then refused at the transport.
    """
    app = make_app(settings, clock, weather_transport)   # no transports at all
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()
    outcome = app.routine_worker.run_one()
    assert outcome.decision == Decision.SEND.value

    report = app.service.tick()

    assert report.notifications_sent == 0
    assert report.notifications_failed == 1
    assert any("delivery failed" in detail for detail in report.details)


# --------------------------------------------------------------------------- #
# Official warnings reaching the briefing (M6)
# --------------------------------------------------------------------------- #
CAP_ALERT = b"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>2.49.0.1.276.0.DWD.1</identifier>
  <sender>opendata@dwd.de</sender>
  <sent>2026-09-06T04:00:00+00:00</sent>
  <status>Actual</status>
  <msgType>Alert</msgType>
  <scope>Public</scope>
  <info>
    <language>en-GB</language>
    <event>SEVERE THUNDERSTORM</event>
    <urgency>Immediate</urgency>
    <severity>Severe</severity>
    <certainty>Likely</certainty>
    <effective>2026-09-06T04:00:00+00:00</effective>
    <expires>2026-09-06T18:00:00+00:00</expires>
    <headline>Official warning of SEVERE THUNDERSTORM</headline>
    <instruction>Secure loose objects and avoid open spaces.</instruction>
    <web>https://www.wettergefahren.de/</web>
    <area>
      <areaDesc>Berlin</areaDesc>
      <polygon>52.3,13.1 52.7,13.1 52.7,13.8 52.3,13.8 52.3,13.1</polygon>
    </area>
  </info>
</alert>"""

ATOM_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><link rel="alternate" href="https://alerts.invalid/one.xml"/></entry>
</feed>"""


class WarningTransport(HttpTransport):
    """Serves forecasts for Open-Meteo and CAP documents for the feed."""

    def __init__(self, payload: dict, *, warnings: bool = True) -> None:
        super().__init__()
        self.payload = payload
        self.warnings = warnings
        self.calls: list[str] = []

    def get(self, url: str, *, params=None) -> HttpResponse:
        self.calls.append(url)
        if url.endswith("one.xml"):
            return HttpResponse(200, CAP_ALERT)
        if "alerts.invalid" in url or "meteoalarm" in url:
            if not self.warnings:
                return HttpResponse(503, b"")
            return HttpResponse(200, ATOM_INDEX)
        return HttpResponse(200, json.dumps(self.payload).encode("utf-8"))


def weather_with_warnings(transport: HttpTransport) -> WeatherService:
    from loop.capabilities.weather.adapters.cap import CapWarningFeed

    return WeatherService(
        transport=transport, locations={"home": BERLIN},
        warning_feeds=[CapWarningFeed(publisher="MeteoAlarm/DE",
                                      index_url="https://alerts.invalid/de",
                                      transport=transport)])


def test_an_official_warning_reaches_the_delivered_briefing(settings, clock, sends):
    """Official wording and attribution survive to the message (weather §5)."""
    transport = WarningTransport(hourly_payload(int(FIRES_AT.timestamp())))
    app = build_application(settings, clock=clock,
                            weather=weather_with_warnings(transport),
                            transports={"telegram": sends})
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()
    app.routine_worker.run_one()
    app.service.tick()

    assert len(sends.sent) == 1
    text = sends.sent[0]["payload"]["text"]
    assert "SEVERE THUNDERSTORM" in text
    assert "Secure loose objects and avoid open spaces." in text
    assert "https://www.wettergefahren.de/" in sends.sent[0]["payload"]["sources"]
    # The forecast itself is mild here; the warning is not averaged away.
    assert "no active warnings" not in text.lower()


def test_a_warning_feed_outage_never_becomes_an_all_clear(settings, clock, sends):
    transport = WarningTransport(hourly_payload(int(FIRES_AT.timestamp())),
                                 warnings=False)
    app = build_application(settings, clock=clock,
                            weather=weather_with_warnings(transport),
                            transports={"telegram": sends})
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()

    outcome = app.routine_worker.run_one()

    assert "Warning state is unknown" in outcome.message
    assert "not an all-clear" in outcome.message
    assert "no active warnings" not in outcome.message.lower()


def test_with_no_feed_configured_the_briefing_says_none_was_checked(
        settings, clock, weather_transport, sends):
    """Unconfigured is a distinct state from checked-and-empty."""
    app = make_app(settings, clock, weather_transport, sends)
    activate_morning_weather(app)
    clock.set(FIRES_AT)
    app.service.tick()

    outcome = app.routine_worker.run_one()

    assert "no warning feed was checked" in outcome.message.lower()
