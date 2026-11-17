"""Learned wake and bed times, phase 2: the coordinator seam, workday lookup and timers (fork Dunky13#1)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.exceptions import HomeAssistantError

from custom_components.climate_advisor import coordinator as co
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.learning import LearningEngine

AMS = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=AMS)  # a Friday
CONFIG = {
    "wake_time": "06:30:00",
    "sleep_time": "22:30:00",
    "preference_learning_enabled": True,
    "schedule_max_shift_min": 90,
    "workday_entity": "binary_sensor.workday",
}


def _coordinator(tmp_path, *, shifts=None, config=None):
    c = object.__new__(ClimateAdvisorCoordinator)
    c.config = {**CONFIG, **(config or {})}
    c.learning = LearningEngine(tmp_path)
    c.learning.load_state()
    c.learning._state.learned_schedule = shifts or {}
    c._preference_band = None
    c._workdays = {}
    c._workday_failed = {}
    c._schedule_unsubs = []
    c._schedule_plan_armed = None
    c._schedule_fired = {}
    c.hass = MagicMock()
    c._zone_scoped = lambda fn: fn
    c._indoor_friendly_name = lambda e: e
    c._async_morning_wakeup = c._async_bedtime = lambda now: None
    return c


def test_nothing_learned_arms_the_clock_timers_exactly_as_before(tmp_path):
    c = _coordinator(tmp_path)
    with patch.object(co, "async_track_time_change") as clock, patch.object(co, "_track_point_in_time") as point:
        c._arm_schedule_timers()
        c._arm_schedule_timers()  # unchanged plan: a no-op
    assert [(call.kwargs["hour"], call.kwargs["minute"]) for call in clock.call_args_list] == [(6, 30), (22, 30)]
    point.assert_not_called()


def test_a_learned_shift_arms_point_in_time_timers_at_the_effective_instants(tmp_path):
    c = _coordinator(tmp_path, shifts={"weekday": {"wake": 30, "bed": 60}, "weekend": {"wake": 0, "bed": 0}})
    with (
        patch.object(co.dt_util, "now", return_value=NOW),
        patch.object(co, "async_track_time_change") as clock,
        patch.object(co, "_track_point_in_time") as point,
    ):
        c._arm_schedule_timers()
    clock.assert_not_called()
    # Friday evening's bed uses Saturday's class (weekend: 0); Saturday's wake is weekend: 0.
    assert [call.args[2] for call in point.call_args_list] == [
        datetime(2026, 11, 21, 6, 30, tzinfo=AMS),
        datetime(2026, 11, 20, 22, 30, tzinfo=AMS),
    ]


def test_a_rearm_after_a_fire_never_fires_the_same_night_twice(tmp_path):
    c = _coordinator(tmp_path, shifts={"weekday": {"wake": 0, "bed": 0}, "weekend": {"wake": 0, "bed": 50}})
    c._schedule_fired["bed"] = datetime(2026, 11, 20, 22, 30, tzinfo=AMS)
    late = datetime(2026, 11, 20, 23, 30, tzinfo=AMS)
    with patch.object(co.dt_util, "now", return_value=late):
        plan = c._schedule_plan(late)
    assert plan.bed > datetime(2026, 11, 21, 9, 0, tzinfo=AMS)  # tomorrow's, not the later learned 23:20 tonight


def test_the_seam_reads_the_learned_time_per_moment_and_the_configured_one_when_off(tmp_path):
    c = _coordinator(tmp_path, shifts={"weekday": {"wake": 0, "bed": 60}, "weekend": {"wake": 0, "bed": 0}})
    # Thursday 22:45 asleep by the configured 22:30, awake under the learned weekday bedtime 23:30.
    thursday = datetime(2026, 11, 19, 22, 45, tzinfo=AMS)
    assert co._asleep_at(c, thursday) is False
    c.config["preference_learning_enabled"] = False
    assert co._asleep_at(c, thursday) is True
    assert co._config_at(c, thursday) is c.config


def test_workdays_resolve_on_the_loop_cache_per_date_and_fall_back_with_one_warning(tmp_path, caplog):
    c = _coordinator(tmp_path)
    calls = []

    async def answer(domain, service, data, **kw):
        calls.append((domain, service, data, kw["target"]))
        if data["check_date"] == "2026-11-20":
            return {"binary_sensor.workday": {"workday": False}}
        if data["check_date"] == "2026-11-21":
            raise HomeAssistantError("entity unavailable")
        return {"binary_sensor.workday": "not a mapping"}

    c.hass.services.async_call = answer
    days = [date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)]
    caplog.set_level("WARNING")
    first = asyncio.run(c.async_resolve_workday_dates(days))
    asyncio.run(c.async_resolve_workday_dates(days))
    assert first == {date(2026, 11, 20): False}
    assert c._workdays == {date(2026, 11, 20): False}
    assert calls[0] == ("workday", "check_date", {"check_date": "2026-11-20"}, {"entity_id": "binary_sensor.workday"})
    assert len(calls) == 3, "a failed date is not asked again within the hour"
    later = co.dt_util.utcnow() + timedelta(hours=1, minutes=1)
    with patch.object(co.dt_util, "utcnow", return_value=later):
        asyncio.run(c.async_resolve_workday_dates(days))
    assert len(calls) == 5
    assert caplog.text.count("Workday unresolved, using the weekend rule") == 2  # once per date, not per retry


def test_a_voluptuous_error_from_the_workday_service_falls_back_instead_of_breaking_the_cycle(tmp_path):
    import voluptuous as vol

    c = _coordinator(tmp_path)

    async def answer(*args, **kw):
        raise vol.Invalid("entity does not exist")

    c.hass.services.async_call = answer
    assert asyncio.run(c.async_resolve_workday_dates([date(2026, 11, 20)])) == {}


def test_a_holiday_makes_a_weekday_a_weekend_day_for_the_schedule(tmp_path):
    c = _coordinator(tmp_path, shifts={"weekday": {"wake": 0, "bed": 0}, "weekend": {"wake": 60, "bed": 0}})
    friday = date(2026, 11, 20)
    c._workdays = {friday: False}
    view = c.preference_view()
    assert view.schedule.effective_times(c.config, friday)[0] == "07:30"
    assert view.schedule.effective_times(c.config, friday - timedelta(days=1))[0] == "06:30:00"


def test_an_invalid_shift_cap_is_replaced_with_one_warning(tmp_path, caplog):
    c = _coordinator(tmp_path)
    c.config["schedule_max_shift_min"] = "ninety"
    caplog.set_level("WARNING")
    assert c.schedule_view().max_shift_min == 90
    c.schedule_view()
    assert caplog.text.count("Invalid schedule_max_shift_min") == 1
