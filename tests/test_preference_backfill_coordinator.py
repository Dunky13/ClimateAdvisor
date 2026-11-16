"""Fork Dunky13#1 phase 3: the history backfill reads the recorder off the event loop; accept and discard the seed.

Occupant outcome: "Analyse history" turns the thermostat's recorded setpoint changes into a proposal the occupant
reviews; nothing changes until Accept, which applies the chosen offsets at once (within the cap). Discard drops it.
"""

from __future__ import annotations

import asyncio
import threading
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor.api import (
    ClimateAdvisorBackfillPreferencesView,
    ClimateAdvisorPreferenceSeedView,
)
from custom_components.climate_advisor.const import DOMAIN
from custom_components.climate_advisor.preference_backfill import climate_rows, humidity_series, temperature_series
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState

CLIMATE = "climate.test_thermostat"
BEDROOM = "sensor.bedroom_temp"
WEATHER = "weather.test_forecast"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
CONFIG = {
    "preference_learning_enabled": True,
    "room_temp_entities": [BEDROOM],
    "wake_time": "06:30",
    "sleep_time": "22:30",
    "temp_unit": "fahrenheit",
}


def _st(at, state, **attrs):
    return FakeState(state=state, attributes=attrs, last_updated=at)


def _history(days: int = 6) -> dict[str, list]:
    """The thermostat's schedule lowers to 64 F at 08:00 every day; the occupant raises to 73 F at a different time."""
    climate, room, weather = [], [], []
    for d in range(days, 0, -1):
        day = NOW - timedelta(days=d)
        raise_at = day.replace(hour=16) + timedelta(minutes=37 * d)
        climate += [
            _st(day.replace(hour=8), "heat", temperature=64.0, current_temperature=65.0),
            _st(raise_at, "heat", temperature=73.0, current_temperature=65.5),
        ]
        room += [_st(raise_at - timedelta(minutes=10), "63.5", unit_of_measurement="°F")]
        weather += [_st(day.replace(hour=h), "cloudy", temperature=t) for h, t in ((6, 38.0), (14, 47.0))]
    return {CLIMATE: climate, BEDROOM: room, WEATHER: weather}


class _Recorder:
    """The recorder seam: ``get_instance(hass).async_add_executor_job`` and ``history.get_significant_states``."""

    def __init__(self, data: dict[str, list]) -> None:
        self.data = data
        self.calls: list[dict] = []

    def module(self) -> types.ModuleType:
        recorder = types.ModuleType("homeassistant.components.recorder")
        history = types.ModuleType("homeassistant.components.recorder.history")

        def get_significant_states(hass, start, end, entity_ids, **kwargs):
            self.calls.append(
                {"thread": threading.current_thread(), "start": start, "end": end, "entities": entity_ids, **kwargs}
            )
            return {e: self.data.get(e, []) for e in entity_ids}

        class _Instance:
            def async_add_executor_job(self, fn, *args):
                return asyncio.get_running_loop().run_in_executor(None, fn, *args)

        history.get_significant_states = get_significant_states
        recorder.history = history
        recorder.get_instance = lambda hass: _Instance()
        return recorder


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder(_history())
    module = rec.module()
    monkeypatch.setitem(__import__("sys").modules, "homeassistant.components.recorder", module)
    monkeypatch.setitem(__import__("sys").modules, "homeassistant.components.recorder.history", module.history)
    return rec


def _coordinator(**config):
    co, fh, sch, _ev = build_headless_coordinator(
        config={**CONFIG, **config},
        climate_state="heat",
        climate_attributes={"current_temperature": 66.0},
        start_time=NOW,
    )
    return co, sch


def _backfill(co, sch, days=7):
    with sch.installed():
        return run_coro(co.async_backfill_preferences(days))


def test_backfill_reads_the_recorder_off_the_event_loop_and_stores_a_seed(recorder):
    co, sch = _coordinator()
    result = _backfill(co, sch)
    [call] = recorder.calls
    assert call["thread"] is not threading.main_thread(), "the recorder read ran on the event loop's thread"
    assert call["significant_changes_only"] is False
    assert set(call["entities"]) == {CLIMATE, BEDROOM, WEATHER}
    assert call["end"] - call["start"] == timedelta(days=7)
    seed = co.learning.preference_seed()
    assert seed is not None and seed["counts"] == result["counts"]
    # Six raises plus five 08:00 schedule lowers (the first row has no change before it); the schedule is removed.
    assert (result["counts"]["changes"], result["counts"]["schedule"], result["counts"]["observations"]) == (11, 5, 6)
    assert all(o["source"] == "history" and o["ref"] is None for o in seed["observations"])
    assert all(BEDROOM in o["rooms"] and "thermostat" in o["rooms"] for o in seed["observations"])
    assert seed["summary"], "the evening raises make a bucket row"
    assert co.learning.preference_offsets() == {}, "nothing applies before Accept"


def test_backfill_is_off_with_the_feature(recorder):
    co, sch = _coordinator(preference_learning_enabled=False)
    assert _backfill(co, sch) is None
    assert recorder.calls == []


def test_accept_applies_the_offsets_now_within_the_cap_and_discard_deletes(recorder):
    co, sch = _coordinator()
    _backfill(co, sch)
    seed = co.learning.preference_seed()
    key = "cool|evening|weekday|-"
    assert seed["summary"][key]["offset_f"] == pytest.approx(3.0), "73 F against the 70 F comfort floor"
    co.config["preference_max_offset_f"] = 1.0  # the occupant lowered the cap before accepting
    with sch.installed():
        result = run_coro(co.async_accept_preference_seed([key]))
    assert result["applied"] == [key]
    assert co.learning.preference_offsets()[key] == pytest.approx(1.0)
    assert co.learning.preference_seed() is None
    _backfill(co, sch)
    with sch.installed():
        assert run_coro(co.async_discard_preference_seed()) is True
        assert run_coro(co.async_discard_preference_seed()) is False
    assert co.learning.preference_seed() is None


# ── the dashboard endpoints ─────────────────────────────────────────────────────────────────────────────────────────


def _request(coordinator, body):
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}
    hass.config.language = "en"
    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {}

    async def _json():
        return body

    request.json = _json
    return request


@pytest.mark.parametrize("days", [0, 401, "30", 2.5, True])
def test_backfill_view_rejects_bad_days(days):
    coordinator = MagicMock()
    resp = asyncio.run(ClimateAdvisorBackfillPreferencesView().post(_request(coordinator, {"days": days})))
    assert resp.status == 400
    coordinator.async_backfill_preferences.assert_not_called()


def test_backfill_view_runs_and_reports_counts(recorder):
    co, sch = _coordinator()
    with sch.installed():
        resp = run_coro(ClimateAdvisorBackfillPreferencesView().post(_request(co, {"days": 7})))
    assert resp.status == 200
    assert resp.json_data["counts"]["observations"] == 6
    assert resp.json_data["message_msg"]["key"] == "errors.preference_seed_built"


@pytest.mark.parametrize("body", [{"action": "keep"}, {"action": "accept", "buckets": "all"}, {"buckets": []}])
def test_seed_view_rejects_bad_bodies(body):
    resp = asyncio.run(ClimateAdvisorPreferenceSeedView().post(_request(MagicMock(), body)))
    assert resp.status == 400


def test_seed_view_reports_a_missing_seed(recorder):
    co, sch = _coordinator()
    with sch.installed():
        resp = run_coro(ClimateAdvisorPreferenceSeedView().post(_request(co, {"action": "discard"})))
    assert resp.status == 404


# ── the recorder rows (pure) ────────────────────────────────────────────────────────────────────────────────────────


def test_recorder_states_become_rows_in_degrees_f():
    at = NOW
    ctx = types.SimpleNamespace(user_id="abc")
    st = FakeState(
        state="heat",
        attributes={"temperature": 20.0, "current_temperature": 19.0, "preset_mode": "eco"},
        last_updated=at,
    )
    st.context = ctx
    [row] = climate_rows([st], "celsius")
    assert (row.mode, row.setpoint_f, row.current_f, row.preset, row.user_id) == ("heat", 68.0, 66.2, "eco", "abc")
    states = [_st(at, "20.0"), _st(at, "unavailable"), _st(at, "x", temperature=10.0)]
    assert temperature_series(states[:2], "celsius") == [(at, 68.0)]
    assert temperature_series(states[2:], "celsius", "temperature") == [(at, 50.0)]
    assert humidity_series([_st(at, "45"), _st(at, "120")]) == [(at, 45.0)]
    assert humidity_series([_st(at, "heat", current_humidity=38)], "current_humidity") == [(at, 38.0)]


# ── the service ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_service_backfills_the_named_zone_and_refuses_while_off(recorder):
    from homeassistant.exceptions import ServiceValidationError

    from tools.sim_harness.build_coordinator import build_headless_multi_zone

    zones, fake_hass, scheduler = build_headless_multi_zone(zone_count=2)
    on, off = zones["zone_0"], zones["zone_1"]
    on["coordinator"].config.update(CONFIG)
    recorder.data = {on["climate_entity"]: recorder.data[CLIMATE], WEATHER: recorder.data[WEATHER]}
    with scheduler.installed():
        run_coro(fake_hass.services.async_call(DOMAIN, "backfill_preferences", {"entry_id": on["entry"].entry_id}))
        assert on["coordinator"].learning.preference_seed() is not None
        assert recorder.calls[0]["end"] - recorder.calls[0]["start"] == timedelta(days=30), "the default window"
        with pytest.raises(ServiceValidationError):
            run_coro(fake_hass.services.async_call(DOMAIN, "backfill_preferences", {"entry_id": off["entry"].entry_id}))
    assert off["coordinator"].learning.preference_seed() is None
