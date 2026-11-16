"""Fork Dunky13#1 phase 3: a room steers the thermostat exactly like the sleep-window bedroom sensor.

Occupant outcome: with preference learning on and a room chosen for a part of the day, Climate Advisor measures the
comfort target in that room and nudges the one thermostat setpoint (the Issue #1194 remote-offset loop: same step, cap,
startup wait and fall-back) so that room gets warm. With the feature off nothing changes.

The real coordinator + engine rig of ``tests/test_remote_offset_gates_1194b.py`` runs a sleep-sensor night and a
room-pinned night side by side; every setpoint write, the watchdog's readings and its learned offset must match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.const import REMOTE_CONTEXT_ROOM, REMOTE_CONTEXT_SLEEP
from custom_components.climate_advisor.indoor_temp import resolve_indoor_temp_with_provenance
from tests.test_remote_offset_gates_1194b import BEDROOM, Night
from tools.sim_harness.fake_hass import FakeState

ROOM_CONFIG = {
    "sleep_indoor_temp_entity": None,
    "preference_learning_enabled": True,
    "room_temp_entities": [BEDROOM],
    "room_pin_night": BEDROOM,
}


def _night(kind: str, **config) -> Night:
    cfg = {**(ROOM_CONFIG if kind == "room" else {}), **config}
    return Night("hvac_fan", "heat", config=cfg)


def _reading(n: Night):
    with n.sch.installed():
        return n.ae._get_indoor_reading()


def _to_cap(n: Night) -> list[float]:
    n.ca_writes()
    n.wait(61)
    n.wait(301)
    return n.sp_writes()


def test_a_pinned_room_drives_the_loop_exactly_like_the_sleep_sensor():
    sleep, room = _night("sleep"), _night("room")
    assert _reading(sleep).remote_context == REMOTE_CONTEXT_SLEEP
    reading = _reading(room)
    assert (reading.remote_context, reading.source_entity) == (REMOTE_CONTEXT_ROOM, BEDROOM)
    assert _to_cap(room) == _to_cap(sleep)
    assert room.sp == room.target + 4.0
    assert room.ae._remote_offset_applied_f == pytest.approx(4.0)
    # The room in band again: both step down the same way.
    for n in (sleep, room):
        n.set_bedroom(n.target + 2.0)
        n.wait(301)
    assert room.sp_writes() == sleep.sp_writes()


def test_a_stale_room_falls_back_to_the_thermostat_like_the_sleep_sensor():
    sleep, room = _night("sleep"), _night("room")
    for n in (sleep, room):
        _to_cap(n)
        n.wait(3 * 3600)  # the bedroom sensor stops reporting
    assert room.sp_writes() == sleep.sp_writes()
    assert room.sp == room.target, "the plain target is back once the room's reading is stale"


def test_feature_off_ignores_the_room():
    n = _night("room", preference_learning_enabled=False)
    assert _reading(n).remote_context is None
    assert not n.ae._remote_sensor_configured()
    _to_cap(n)
    assert n.sp == n.target, f"no offset with preference learning off: {n.sp_writes()}"


def test_auto_without_a_learned_room_keeps_the_thermostat():
    n = _night("room", room_pin_night="auto")
    assert _reading(n).remote_context is None
    assert n.ae._remote_sensor_configured(), "the loop is armed: a room may be learned later"
    _to_cap(n)
    assert n.sp == n.target


def test_watchdog_sees_the_same_readings_and_offset_for_room_and_sleep():
    sleep, room = _night("sleep"), _night("room")
    for n in (sleep, room):
        _to_cap(n)
        offsets = {"cold|night|weekday|-": {"offset_f": 1.0}, "cold|night|weekday|dry": {"offset_f": 2.0}}
        n.co.learning._state.preference_offsets = offsets
        n.co._preference_band = "dry"
    now = room.sch.now()
    # The promise checks (P1 settling, P7 comfort kept, ...) are pure functions of these readings and that offset.
    assert room.co._promise_reading_now(now) == sleep.co._promise_reading_now(now)
    # Room steering needs preference learning on; give the sleep night the same switch for the offset comparison.
    sleep.co.config["preference_learning_enabled"] = True
    assert room.co._promise_heat_offset(now, True) == sleep.co._promise_heat_offset(now, True) == 2.0
    # The band the engine arms carries the same four-part offset the watchdog judges against.
    with room.sch.installed():
        band = room.ae._resolved_comfort_band(room.ae._current_classification)
    assert band.floor == pytest.approx(room.co.config["sleep_heat"] + 2.0)


def test_observation_carries_the_room_reference_rooms_and_band():
    n = _night("room")
    state = n.fh.states.get(n.ce)
    n.fh.states.set(n.ce, FakeState(state=state.state, attributes={**state.attributes, "current_humidity": 35}))
    n.co._occupancy_mode = "home"
    n.co._automation_enabled = True
    n.co.config["sleep_time"], n.co.config["wake_time"] = "20:30", "06:30"
    _to_cap(n)
    n.wait(150)
    n.occupant_dials(n.sp + 1.0)
    episode = n.co.learning._state.preference_open_episode
    assert episode is not None, "the occupant's change opened an episode"
    assert episode["ref"] == BEDROOM
    assert episode["band"] == "dry" and episode["rh"] == 35.0
    assert set(episode["rooms"]) == {BEDROOM, "thermostat"}
    assert episode["bucket"].endswith("|dry")


def test_reconcile_restarts_a_part_whose_pin_changed():
    n = _night("room")
    co = n.co
    co.learning._state.preference_offsets = {"cold|night|weekday|-": {"offset_f": 1.0}}
    co.learning._state.learned_rooms = {"night": {"entity": None, "ratios": {}, "ref": None}}
    assert co._reconcile_preference_references()
    assert co.learning.preference_offsets() == {}, "night moved from the thermostat to the bedroom: restart from 0"
    assert co.learning._state.learned_rooms["night"]["ref"] == BEDROOM


def test_a_stale_pin_reads_as_auto_with_a_warning(caplog):
    n = _night("room", room_pin_morning="sensor.gone")
    caplog.set_level("WARNING")
    n.co._reconcile_preference_references()
    assert "not a selected room sensor; using auto" in caplog.text


# ── the resolver on its own ─────────────────────────────────────────────────────────────────────────────────────────


class _States:
    def __init__(self, states: dict) -> None:
        self._s = states

    def get(self, entity_id):
        return self._s.get(entity_id)


def _hass(mode: str, modes: list[str], room_value: str = "64.0", reported: datetime | None = None):
    now = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    room = FakeState(state=room_value, attributes={"unit_of_measurement": "°F"})
    room.last_reported = room.last_updated = reported or now
    climate = FakeState(state=mode, attributes={"current_temperature": 68.0, "hvac_modes": modes})
    return SimpleNamespace(states=_States({"climate.t": climate, BEDROOM: room})), now


def _resolve(hass, now, room=BEDROOM):
    return resolve_indoor_temp_with_provenance(
        hass=hass,
        source="climate_fallback",
        unit="fahrenheit",
        indoor_temp_entity=None,
        climate_entity="climate.t",
        now=now,
        room_entity=room,
    )


def test_resolver_uses_the_room_while_heating():
    r = _resolve(*_hass("heat", ["off", "heat", "cool"]))
    assert (r.value, r.source_entity, r.primary_value, r.remote_context, r.room_reason) == (
        64.0,
        BEDROOM,
        68.0,
        REMOTE_CONTEXT_ROOM,
        None,
    )


def test_resolver_heat_only_thermostat_off_still_uses_the_room():
    assert _resolve(*_hass("off", ["off", "heat"])).remote_context == REMOTE_CONTEXT_ROOM


@pytest.mark.parametrize("mode", ["cool", "off", "heat_cool"])
def test_resolver_never_steers_by_a_room_when_the_thermostat_can_cool(mode):
    r = _resolve(*_hass(mode, ["off", "heat", "cool", "heat_cool"]))
    assert (r.value, r.remote_context, r.room_reason) == (68.0, None, "not_heating")


def test_resolver_falls_back_on_a_stale_or_broken_room():
    hass, now = _hass("heat", ["off", "heat"], reported=datetime(2026, 11, 15, 8, 0, tzinfo=UTC))
    assert _resolve(hass, now).room_reason == "stale"
    hass, now = _hass("heat", ["off", "heat"], room_value="unavailable")
    r = _resolve(hass, now)
    assert (r.value, r.remote_context, r.room_reason) == (68.0, None, "unavailable")
