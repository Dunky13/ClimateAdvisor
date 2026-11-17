"""Real-HA test: the automation-caused predicate resolves entity, device and area targets (fork Dunky13#1, phase 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import Context, Event
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor.automation_calls import AutomationCalls, targeted_entities


async def test_entity_device_and_area_targets_all_reach_the_thermostat_entity(hass):
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Bedroom")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "nest")}, name="Nest"
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    er.async_get(hass).async_get_or_create("climate", "test", "nest", device_id=device.id, suggested_object_id="nest")

    assert targeted_entities(hass, {"entity_id": ["climate.nest"]}) == {"climate.nest"}
    assert targeted_entities(hass, {"device_id": device.id}) == {"climate.nest"}
    assert targeted_entities(hass, {"area_id": area.id}) == {"climate.nest"}
    assert targeted_entities(hass, {"temperature": 18}) == frozenset()


async def test_a_parent_context_call_to_a_device_marks_the_entity_for_two_minutes(hass):
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "nest")}, name="Nest"
    )
    er.async_get(hass).async_get_or_create("climate", "test", "nest", device_id=device.id, suggested_object_id="nest")
    fired = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    calls = AutomationCalls()
    calls.record(
        hass,
        Event(
            "call_service",
            {"domain": "climate", "service": "set_temperature", "service_data": {"device_id": device.id}},
            context=Context(parent_id="run-1"),
            time_fired_timestamp=fired.timestamp(),
        ),
    )
    assert calls.caused("climate.nest", Context(), fired + timedelta(seconds=30))
    assert not calls.caused("climate.nest", Context(), fired + timedelta(seconds=130))


async def _setup_light_and_time_automation(hass):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"tv_led": {"initial": True}}})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {"platform": "time", "at": "23:59:00"},
                "action": {"service": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.tv_led"}},
            }
        },
    )
    await hass.async_block_till_done()


async def _stop_automation(hass):
    """Cancel the automation's time listener so the test leaves no timer behind."""
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)


def _track(hass):
    """An AutomationCalls fed by the same events the coordinator listens to, and the verdict per state change."""
    from homeassistant.util import dt as dt_util

    from custom_components.climate_advisor.automation_calls import RUN_EVENTS

    calls, verdicts = AutomationCalls(), []
    hass.bus.async_listen("call_service", lambda event: calls.record(hass, event))
    for run_event in RUN_EVENTS:
        hass.bus.async_listen(run_event, calls.record_run)

    def _changed(event):
        new = event.data["new_state"]
        if event.data["entity_id"] == "input_boolean.tv_led" and event.data["old_state"] is not None:
            verdicts.append(calls.caused("input_boolean.tv_led", new.context, dt_util.utcnow()))

    hass.bus.async_listen("state_changed", _changed)
    return verdicts


async def test_a_time_triggered_automation_turning_a_light_off_is_automation_caused(hass):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await _setup_light_and_time_automation(hass)
    verdicts = _track(hass)
    now = dt_util.now()
    async_fire_time_changed(hass, now.replace(hour=23, minute=59, second=1) + timedelta(days=1))
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.tv_led").state == "off"
    assert verdicts == [True]
    await _stop_automation(hass)


async def test_a_plain_service_call_toggling_the_light_is_not_automation_caused(hass):
    await _setup_light_and_time_automation(hass)
    verdicts = _track(hass)
    await hass.services.async_call(
        "input_boolean", "turn_off", {"entity_id": "input_boolean.tv_led"}, blocking=True, context=Context()
    )
    assert verdicts == [False]
    await _stop_automation(hass)
