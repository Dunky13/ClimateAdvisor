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
