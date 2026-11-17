"""Fork Dunky13#1 phase 3: what the occupant sees and sets for rooms, bands and the history seed.

Occupant outcomes: the options form refuses a pin to a room that is not selected; the briefing names the room a
learned offset is measured in; the Status line says what Climate Advisor steers by and the humidity band; the seed
review shows medians and offsets in the user's unit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.api import _preference_status_payload, _preferences_payload
from custom_components.climate_advisor.briefing import _generate_tldr_table
from custom_components.climate_advisor.preference_learning import PreferenceView
from tests.test_config_flow import FULL_CONFIG, _make_options_flow
from tests.test_preference_learning import CONFIG, _classification

BEDROOM = "sensor.bedroom_temp"
B = "cold|evening|weekday"

# ── options flow ────────────────────────────────────────────────────────────────────────────────────────────────────


def _submit(**fields):
    flow, captured = _make_options_flow(dict(FULL_CONFIG))
    user_input = {
        "preference_learning_enabled": True,
        "schedule_max_shift_min": 90,
        "schedule_weekly_step_min": 15,
        "room_pin_night": "auto",
        "room_pin_morning": "auto",
        "room_pin_afternoon": "auto",
        "room_pin_evening": "auto",
        **fields,
    }
    with patch("custom_components.climate_advisor.config_flow.ir.async_create_issue"):
        result = asyncio.run(flow.async_step_preference_learning(user_input))
    return result, captured


def test_options_refuse_a_pin_to_a_room_that_is_not_selected():
    result, captured = _submit(room_temp_entities=[], room_pin_night=BEDROOM)
    assert result["errors"] == {"room_pin_night": "room_pin_not_selected"}
    assert "data" not in captured, "nothing is saved while a pin is invalid"


def test_options_accept_a_pin_to_a_room_selected_in_the_same_form():
    _result, captured = _submit(room_temp_entities=[BEDROOM], room_pin_night=BEDROOM, room_pin_morning="thermostat")
    data = captured["data"]
    assert data["room_temp_entities"] == [BEDROOM]
    assert (data["room_pin_night"], data["room_pin_morning"]) == (BEDROOM, "thermostat")


# ── briefing ────────────────────────────────────────────────────────────────────────────────────────────────────────


def _tldr(view, hvac_mode="heat"):
    config = {
        **CONFIG,
        "sleep_time": time(22, 30),
        "wake_time": time(6, 30),
        "temp_unit": "fahrenheit",
        "room_temp_entities": [BEDROOM],
    }
    c = _classification(hvac_mode=hvac_mode)
    return "\n".join(_generate_tldr_table(c, config, now=datetime(2026, 11, 16, 18, 0, tzinfo=UTC), preferences=view))


def test_briefing_names_the_room_a_learned_offset_is_measured_in():
    view = PreferenceView({B: 2.0}, learned_rooms={"evening": BEDROOM}, room_names={BEDROOM: "Bedroom"})
    assert "evening +2.0°F in Bedroom" in _tldr(view)
    # The thermostat as the reference: the line is unchanged.
    assert "evening +2.0°F in" not in _tldr(PreferenceView({B: 2.0}))
    # D5: a room never steers cooling, so a cooling day never names it.
    assert "in Bedroom" not in _tldr(view, hvac_mode="cool")


# ── Status line and seed payload ────────────────────────────────────────────────────────────────────────────────────


def _status(reference, band="dry"):
    coord = MagicMock()
    coord.preference_status.return_value = {
        "enabled": True,
        "bucket": f"{B}|{band}",
        "active_offset_f": 0.0,
        "observation_count": 3,
        "last_update": None,
        "band": band,
        "reference": reference,
    }
    hass = MagicMock()
    hass.config.language = "en"
    return _preference_status_payload(hass, coord, "celsius")


def test_status_line_names_the_steering_room_its_source_and_the_band():
    ref = {"entity": BEDROOM, "name": "Bedroom", "source": "learned", "in_use": True, "value_f": 67.28}
    payload = _status({**ref, "remote_offset_f": 0.9})
    assert payload["ref_text"] == "Steering by Bedroom (learned) 19.6°C, thermostat +0.5°C. Humidity: dry."
    assert payload["band"] == "dry"


def test_status_line_says_thermostat_while_the_room_is_not_in_use():
    ref = {"entity": BEDROOM, "name": "Bedroom", "source": "pinned", "in_use": False, "value_f": None}
    assert _status(ref, band="-")["ref_text"] == "Steering by the thermostat. Humidity: unknown."


def test_seed_rows_show_median_and_offset_in_the_users_unit():
    coord = MagicMock()
    coord.preference_status.return_value = {
        "enabled": True,
        "bucket": None,
        "active_offset_f": 0.0,
        "buckets": [],
        "seed": {"rows": [{"bucket": f"{B}|-", "n": 4, "median_f": 3.6, "offset_f": 1.8, "disabled": False}]},
    }
    payload = _preferences_payload(coord, "celsius")
    [row] = payload["seed"]["rows"]
    assert (row["median"], row["offset"]) == (pytest.approx(2.0), pytest.approx(1.0))


def test_with_a_room_reference_chart_briefing_and_written_floor_agree():
    """Spec consistency: a room reference changes where the target is measured, never the target itself."""
    from custom_components.climate_advisor import coordinator as coord_mod
    from custom_components.climate_advisor.automation import select_comfort_band
    from custom_components.climate_advisor.coordinator import _compute_target_band_schedule

    view = PreferenceView({B: 2.0}, learned_rooms={"evening": BEDROOM}, room_names={BEDROOM: "Bedroom"})
    config = {**CONFIG, "temp_unit": "fahrenheit", "room_temp_entities": [BEDROOM]}
    c = _classification()
    ts = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)
    with patch.object(coord_mod.dt_util, "as_local", side_effect=lambda x: x):
        chart = _compute_target_band_schedule([ts], config, "home", ts, classification=c, preference_view=view)[0]
    written = select_comfort_band(
        c, config, occupancy_mode="home", in_sleep_window=False, aggressive_savings=False, at=ts, preferences=view
    )
    assert chart["lower"] == pytest.approx(written.floor) == config["comfort_heat"] + 2.0
    table = _tldr(view)
    assert "evening +2.0°F in Bedroom" in table
    assert f"{written.floor:.0f}" in table, "the briefing quotes the floor the thermostat is armed with"
