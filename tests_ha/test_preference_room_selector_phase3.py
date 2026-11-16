"""Fork Dunky13#1 phase 3: the options-flow room picker offers temperature sensors and input_number helpers.

The remote reader accepts both, so a room kept in an ``input_number`` helper must be selectable. HA applies an
entity selector's filter in the frontend, so the serialised filter is what decides what the picker lists.
"""

from __future__ import annotations


async def test_room_picker_offers_temperature_sensors_and_input_number_helpers(make_house) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry = house.hass, house.ca_entry
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "preference_learning"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "preference_learning"

    picker = next(v for k, v in result["data_schema"].schema.items() if str(k) == "room_temp_entities")
    entity = picker.serialize()["selector"]["entity"]
    assert entity["multiple"] is True
    domains = {(d, f.get("device_class") and tuple(f["device_class"])) for f in entity["filter"] for d in f["domain"]}
    assert domains == {("sensor", ("temperature",)), ("input_number", None)}

    room = "input_number.bedroom_temp"
    hass.states.async_set(room, "64", {"unit_of_measurement": "°F"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"preference_learning_enabled": True, "room_temp_entities": [room], "room_pin_night": room},
    )
    # A saved section flushes to the entry and returns to the options menu.
    assert result["type"] is FlowResultType.MENU, result.get("errors")
    assert (entry.data["room_temp_entities"], entry.data["room_pin_night"]) == ([room], room)
