"""Learned wake and bed times, phase 2: what the occupant sees in settings, Status, Analysis and the briefing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor.api import _preference_status_payload
from custom_components.climate_advisor.briefing import _generate_tldr_table
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.preference_learning import PreferenceView
from custom_components.climate_advisor.preference_schedule import ScheduleView
from tests.test_config_flow import FULL_CONFIG, _make_options_flow
from tests.test_preference_learning import CONFIG, _classification

NOW = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)  # a Monday: tomorrow is a weekday
SHIFTS = {"weekday": {"wake": 35, "bed": 80}, "weekend": {"wake": 0, "bed": 0}}


def _submit(**fields):
    flow, captured = _make_options_flow({**FULL_CONFIG})
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
    with (
        patch("custom_components.climate_advisor.config_flow.ir.async_create_issue"),
        patch.object(flow.hass.states, "get", side_effect=lambda e: None if e == "light.gone" else object()),
    ):
        result = asyncio.run(flow.async_step_preference_learning(user_input))
    return result, captured


def test_options_save_the_signal_entities_and_the_schedule_limits():
    _result, captured = _submit(wake_signal_entities=["light.bed"], bed_signal_entities=["light.bed"])
    data = captured["data"]
    assert data["wake_signal_entities"] == data["bed_signal_entities"] == ["light.bed"]
    assert (data["schedule_max_shift_min"], data["schedule_weekly_step_min"]) == (90, 15)


def test_options_refuse_a_signal_entity_that_does_not_exist():
    result, captured = _submit(wake_signal_entities=["light.gone"])
    assert result["errors"] == {"wake_signal_entities": "signal_entity_not_found"}
    assert "data" not in captured


def _view(shifts=SHIFTS):
    return PreferenceView(schedule=ScheduleView(shifts=shifts, max_shift_min=90))


def _tldr(view):
    config = {**CONFIG, "sleep_time": time(22, 30), "wake_time": time(6, 30), "temp_unit": "fahrenheit"}
    return "\n".join(_generate_tldr_table(_classification(), config, now=NOW, preferences=view))


def test_briefing_names_tomorrows_learned_wake_only_when_it_differs():
    assert "Wake Tomorrow: 7:05 AM (learned)" in _tldr(_view())
    assert "Wake Tomorrow" not in _tldr(_view({"weekday": {"wake": 0, "bed": 80}, "weekend": {"wake": 0, "bed": 0}}))
    assert "Wake Tomorrow" not in _tldr(PreferenceView())


def _status_coordinator(tmp_path):
    c = object.__new__(ClimateAdvisorCoordinator)
    c.config = {"wake_time": "06:30:00", "sleep_time": "22:30:00"}
    c.learning = LearningEngine(tmp_path)
    c.learning.load_state()
    return c


def test_the_status_block_reports_tomorrows_times_and_each_pairs_configured_time(tmp_path):
    c = _status_coordinator(tmp_path)
    block = c._preference_schedule_status(ScheduleView(shifts=SHIFTS, max_shift_min=90), NOW)
    assert (block["tomorrow_class"], block["wake"], block["bed"]) == ("weekday", "07:05", "23:50")
    assert (block["wake_shift_min"], block["bed_shift_min"]) == (35, 80)
    assert {(r["class"], r["kind"]): r["configured"] for r in block["rows"]}[("weekday", "bed")] == "22:30"


def test_the_status_line_appears_only_while_a_shift_applies():
    schedule = {
        "active": True,
        "tomorrow_class": "weekday",
        "wake": "07:05",
        "bed": "23:50",
        "wake_shift_min": 35,
        "bed_shift_min": -80,
    }
    hass = MagicMock()
    hass.config.language = "en"
    coord = MagicMock()
    coord.preference_status.return_value = {"enabled": True, "bucket": None, "schedule": schedule}
    text = _preference_status_payload(hass, coord, "celsius")["schedule_text"]
    assert text == ("Learned times: wake tomorrow 07:05 (+35 min), bed tonight 23:50 (-80 min). Tomorrow is a weekday.")
    coord.preference_status.return_value = {"enabled": True, "bucket": None, "schedule": {**schedule, "active": False}}
    assert "schedule_text" not in _preference_status_payload(hass, coord, "celsius")
