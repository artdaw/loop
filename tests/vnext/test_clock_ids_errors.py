"""Clock, identity and failure-taxonomy foundations (runtime §3, §10)."""

from __future__ import annotations

import datetime as dt

import pytest

from loop.core.clock import (
    UTC,
    FrozenClock,
    SystemClock,
    from_micros,
    local_date,
    parse_rfc3339,
    to_micros,
    to_rfc3339,
)
from loop.core.errors import Conflict, ErrorCode, LoopError, NeedsClarification
from loop.core.ids import content_hash, input_hash, new_id

MOMENT = dt.datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #
def test_frozen_clock_does_not_move_on_its_own():
    clock = FrozenClock(MOMENT)
    assert clock.now() == clock.now() == MOMENT


def test_frozen_clock_advances_only_when_told():
    clock = FrozenClock(MOMENT)
    clock.advance(hours=2)
    assert clock.now() == MOMENT + dt.timedelta(hours=2)


def test_frozen_clock_refuses_to_go_backwards():
    """Time travel would silently invalidate lease and cooldown reasoning."""
    clock = FrozenClock(MOMENT)
    with pytest.raises(ValueError):
        clock.advance(seconds=-1)


def test_frozen_clock_can_jump_for_restart_scenarios():
    clock = FrozenClock(MOMENT)
    later = MOMENT + dt.timedelta(days=3)
    assert clock.set(later) == later


def test_frozen_clock_requires_an_aware_datetime():
    with pytest.raises(ValueError):
        FrozenClock(dt.datetime(2026, 9, 5, 7, 0))


def test_system_clock_returns_aware_utc():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)


# --------------------------------------------------------------------------- #
# Instant storage
# --------------------------------------------------------------------------- #
def test_instants_round_trip_through_integer_micros():
    assert from_micros(to_micros(MOMENT)) == MOMENT


def test_micros_are_integers_not_floats():
    assert isinstance(to_micros(MOMENT), int)


def test_naive_datetimes_cannot_be_stored():
    with pytest.raises(ValueError):
        to_micros(dt.datetime(2026, 9, 5, 7, 0))


def test_rfc3339_uses_a_z_suffix():
    assert to_rfc3339(MOMENT) == "2026-09-05T07:00:00Z"


def test_rfc3339_round_trips():
    assert parse_rfc3339(to_rfc3339(MOMENT)) == MOMENT


def test_non_utc_input_is_normalised_for_output():
    berlin = MOMENT.astimezone(dt.timezone(dt.timedelta(hours=2)))
    assert to_rfc3339(berlin) == "2026-09-05T07:00:00Z"


def test_local_date_is_a_plain_iso_day_string():
    """Day-level obligations stay dates; runtime §3 forbids UTC-midnight coercion."""
    late = dt.datetime(2026, 9, 5, 23, 30, tzinfo=UTC)
    assert local_date(late, "Europe/Berlin") == "2026-09-06"
    assert local_date(late, "UTC") == "2026-09-05"


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_new_id_is_unique():
    assert len({new_id() for _ in range(200)}) == 200


def test_content_hash_is_stable_across_str_and_bytes():
    assert content_hash("hello") == content_hash(b"hello")


def test_input_hash_ignores_key_order():
    assert input_hash({"a": 1, "b": 2}) == input_hash({"b": 2, "a": 1})


def test_input_hash_changes_with_the_payload():
    """An approval bound to one payload must not authorise a different one."""
    assert input_hash({"to": "a@b.c"}) != input_hash({"to": "evil@x.y"})


def test_input_hash_distinguishes_nested_changes():
    assert input_hash({"m": {"x": 1}}) != input_hash({"m": {"x": 2}})


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_error_carries_its_code():
    assert Conflict("clash").code is ErrorCode.CONFLICT


def test_cli_exit_codes_follow_the_interface_contract():
    assert NeedsClarification("when?").exit_code == 3
    assert Conflict("clash").exit_code == 5
    assert LoopError("boom").exit_code == 1


def test_error_envelope_shape():
    envelope = Conflict("version mismatch",
                        details={"expected_version": 3}).to_envelope("req-1")

    assert envelope["error"]["code"] == "conflict"
    assert envelope["error"]["details"] == {"expected_version": 3}
    assert envelope["request_id"] == "req-1"


def test_every_taxonomy_code_has_an_exit_code():
    from loop.core.errors import EXIT_CODES

    assert set(EXIT_CODES) == set(ErrorCode)
