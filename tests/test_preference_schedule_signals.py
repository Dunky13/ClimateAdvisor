"""Learned wake and bed times, phase 2: the automation-caused predicate and live candidates (fork Dunky13#1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import automation_calls as ac
from custom_components.climate_advisor import coordinator as co
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.learning import LearningEngine

AMS = ZoneInfo("Europe/Amsterdam")
UTC = UTC
LIGHT = "light.bedroom"
CLIMATE = "climate.nest"
CONFIG = {
    "wake_time": "07:00:00",
    "sleep_time": "23:00:00",
    "climate_entity": CLIMATE,
    "preference_learning_enabled": True,
    "wake_signal_entities": [LIGHT],
    "bed_signal_entities": [LIGHT],
}


def _context(parent_id=None):
    return SimpleNamespace(id="ctx", parent_id=parent_id)


def _call(at, parent_id="run-1"):
    return SimpleNamespace(context=_context(parent_id), time_fired=at, data={"service_data": {"entity_id": "x"}})


def test_a_parent_context_is_automation_caused_on_its_own():
    calls = ac.AutomationCalls()
    now = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
    assert calls.caused(LIGHT, _context("run-1"), now)
    assert not calls.caused(LIGHT, _context(None), now)
    assert not calls.caused(LIGHT, None, now)


def test_a_recent_call_service_with_a_parent_context_covers_the_entities_it_targets():
    calls = ac.AutomationCalls()
    fired = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    with patch.object(ac, "targeted_entities", return_value=frozenset({CLIMATE})):
        calls.record(MagicMock(), _call(fired))
        calls.record(MagicMock(), _call(fired, parent_id=None))  # a person's call is not remembered
    assert calls.caused(CLIMATE, _context(None), fired + timedelta(seconds=30))
    assert not calls.caused(LIGHT, _context(None), fired + timedelta(seconds=30))
    assert not calls.caused(CLIMATE, _context(None), fired + timedelta(seconds=121))


def test_a_recorded_run_context_covers_its_state_changes_and_its_service_calls_for_two_minutes():
    calls = ac.AutomationCalls()
    fired = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    run = SimpleNamespace(context=SimpleNamespace(id="run-ctx", parent_id=None), time_fired=fired)
    calls.record_run(run)
    own = SimpleNamespace(id="run-ctx", parent_id=None)
    assert calls.caused(LIGHT, own, fired + timedelta(seconds=1))
    assert calls.caused(LIGHT, SimpleNamespace(id="x", parent_id="run-ctx"), fired + timedelta(seconds=1))
    assert not calls.caused(LIGHT, SimpleNamespace(id="other", parent_id=None), fired + timedelta(seconds=1))
    assert not calls.caused(LIGHT, own, fired + timedelta(seconds=121))
    assert calls._runs == {}  # pruned


def test_a_call_service_under_a_run_context_marks_its_targets():
    calls = ac.AutomationCalls()
    fired = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    calls.record_run(SimpleNamespace(context=SimpleNamespace(id="run-ctx", parent_id=None), time_fired=fired))
    call = SimpleNamespace(
        context=SimpleNamespace(id="svc", parent_id="run-ctx"),
        time_fired=fired,
        data={"service_data": {"entity_id": "x"}},
    )
    with patch.object(ac, "targeted_entities", return_value=frozenset({CLIMATE})):
        calls.record(MagicMock(), call)
    assert calls.caused(CLIMATE, _context(None), fired + timedelta(seconds=30))


def test_the_run_memory_is_bounded():
    calls = ac.AutomationCalls()
    fired = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    for n in range(ac._MAX_RUNS + 5):
        calls.record_run(SimpleNamespace(context=SimpleNamespace(id=f"r{n}", parent_id=None), time_fired=fired))
    assert len(calls._runs) == ac._MAX_RUNS


def test_a_call_without_a_parent_context_is_never_resolved():
    calls = ac.AutomationCalls()
    with patch.object(ac, "targeted_entities") as resolve:
        calls.record(MagicMock(), _call(datetime(2026, 11, 20, 23, 0, tzinfo=UTC), parent_id=None))
    resolve.assert_not_called()


def _coordinator(tmp_path, *, config=None, occupancy="home"):
    c = object.__new__(ClimateAdvisorCoordinator)
    c.config = {**CONFIG, **(config or {})}
    c.learning = LearningEngine(tmp_path)
    c.learning.load_state()
    c._workdays = {}
    c._preference_band = None
    c._occupancy_mode = occupancy
    c._automation_calls = ac.AutomationCalls()
    c._executor_job = AsyncMock()
    c.hass = MagicMock()
    c._indoor_friendly_name = lambda e: e
    return c


def _event(entity, old, new, parent_id=None):
    return SimpleNamespace(
        data={
            "entity_id": entity,
            "old_state": None if old is None else SimpleNamespace(state=old),
            "new_state": SimpleNamespace(state=new, context=_context(parent_id)),
        }
    )


def _signal(c, event, at):
    with patch.object(co.dt_util, "now", return_value=at), patch.object(co.dt_util, "utcnow", return_value=at):
        asyncio.run(c._async_schedule_signal(event))


def test_a_morning_activation_stores_a_wake_candidate_and_saves(tmp_path):
    c = _coordinator(tmp_path, config={"bed_signal_entities": []})
    _signal(c, _event(LIGHT, "off", "on"), datetime(2026, 11, 20, 7, 12, tzinfo=AMS))
    assert [(x["kind"], x["date"], x["delta_min"], x["source"]) for x in c.learning.schedule_candidates()] == [
        ("wake", "2026-11-20", 12, "sensor")
    ]
    c._executor_job.assert_awaited_once_with(c.learning.save_state)


@pytest.mark.parametrize(
    ("old", "new"), [("unavailable", "on"), ("unknown", "on"), (None, "on"), ("on", "unavailable"), ("on", "on")]
)
def test_unavailable_unknown_and_missing_states_never_count(tmp_path, old, new):
    c = _coordinator(tmp_path, config={"bed_signal_entities": []})
    _signal(c, _event(LIGHT, old, new), datetime(2026, 11, 20, 7, 12, tzinfo=AMS))
    assert c.learning.schedule_candidates() == []
    c._executor_job.assert_not_awaited()


def test_a_bedtime_after_midnight_belongs_to_the_previous_evening_and_the_last_off_wins(tmp_path):
    c = _coordinator(tmp_path, config={"wake_signal_entities": []})
    _signal(c, _event(LIGHT, "on", "off"), datetime(2026, 11, 20, 23, 15, tzinfo=AMS))
    _signal(c, _event(LIGHT, "on", "off"), datetime(2026, 11, 21, 0, 20, tzinfo=AMS))
    assert [(x["date"], x["delta_min"]) for x in c.learning.schedule_candidates()] == [("2026-11-21", 80)]


def test_an_automation_caused_signal_is_skipped_by_context_and_by_a_recent_service_call(tmp_path, caplog):
    c = _coordinator(tmp_path, config={"bed_signal_entities": []})
    caplog.set_level("INFO")
    at = datetime(2026, 11, 20, 7, 12, tzinfo=AMS)
    _signal(c, _event(LIGHT, "off", "on", parent_id="run-1"), at)
    with patch.object(ac, "targeted_entities", return_value=frozenset({LIGHT})):
        c._automation_calls.record(c.hass, _call(at.astimezone(UTC) - timedelta(seconds=30)))
    _signal(c, _event(LIGHT, "off", "on"), at)
    assert c.learning.schedule_candidates() == []
    assert "Schedule signal skipped: reason=caused by an automation" in caplog.text


def test_a_signal_while_away_is_not_a_candidate(tmp_path, caplog):
    c = _coordinator(tmp_path, config={"bed_signal_entities": []}, occupancy="away")
    caplog.set_level("INFO")
    _signal(c, _event(LIGHT, "off", "on"), datetime(2026, 11, 20, 7, 12, tzinfo=AMS))
    assert c.learning.schedule_candidates() == []
    assert "Schedule signal skipped: reason=occupancy away" in caplog.text


def test_an_event_entity_counts_each_new_event_for_both_kinds(tmp_path):
    c = _coordinator(
        tmp_path, config={"wake_signal_entities": ["event.button"], "bed_signal_entities": ["event.button"]}
    )
    _signal(
        c,
        _event("event.button", "2026-11-20T06:00:00", "2026-11-20T07:05:00"),
        datetime(2026, 11, 20, 7, 5, tzinfo=AMS),
    )
    assert [x["kind"] for x in c.learning.schedule_candidates()] == ["wake"]


def test_an_event_after_a_restart_counts_when_live_but_not_the_restored_last_event(tmp_path):
    c = _coordinator(tmp_path, config={"bed_signal_entities": [], "wake_signal_entities": ["event.button"]})
    at = datetime(2026, 11, 20, 7, 5, 30, tzinfo=AMS)
    _signal(c, _event("event.button", None, "2026-11-19T22:00:00+00:00"), at)  # the restore at startup
    _signal(c, _event("event.button", "unknown", "2026-11-19T22:00:00+00:00"), at)
    assert c.learning.schedule_candidates() == []
    _signal(c, _event("event.button", "unknown", at.astimezone(UTC).isoformat()), at)  # a press just now
    assert [x["kind"] for x in c.learning.schedule_candidates()] == ["wake"]


def test_the_tv_led_pattern_keeps_the_hand_offs_in_the_collector(tmp_path):
    """A person's off at 23:54 counts; the automation's 23:59 is filtered by its context."""
    c = _coordinator(tmp_path, config={"wake_signal_entities": []})
    _signal(c, _event(LIGHT, "on", "off"), datetime(2026, 11, 20, 23, 54, tzinfo=AMS))
    _signal(c, _event(LIGHT, "on", "off", parent_id="run-1"), datetime(2026, 11, 20, 23, 59, tzinfo=AMS))
    assert [(x["date"], x["delta_min"]) for x in c.learning.schedule_candidates()] == [("2026-11-21", 54)]


def test_the_thermostat_fallback_applies_only_to_a_kind_without_signal_entities(tmp_path):
    c = _coordinator(tmp_path, config={"wake_signal_entities": [], "bed_signal_entities": [LIGHT]})
    schedule = {"wake_time": "07:00:00", "sleep_time": "23:00:00"}
    at = datetime(2026, 11, 20, 6, 20, tzinfo=AMS)
    assert c._record_thermostat_schedule_candidate(at, schedule, 66.0, 68.0)
    assert [(x["kind"], x["delta_min"], x["source"]) for x in c.learning.schedule_candidates()] == [
        ("wake", -40, "thermostat")
    ]
    late = datetime(2026, 11, 20, 22, 30, tzinfo=AMS)
    assert not c._record_thermostat_schedule_candidate(late, schedule, 68.0, 64.0)  # bed has a signal entity
    assert not c._record_thermostat_schedule_candidate(at, schedule, 68.0, 68.0)
    assert not c._record_thermostat_schedule_candidate(datetime(2026, 11, 20, 12, 0, tzinfo=AMS), schedule, 66.0, 68.0)


def test_the_live_observation_path_skips_an_automation_caused_thermostat_change(tmp_path, caplog):
    c = _coordinator(tmp_path)
    caplog.set_level("INFO")
    old = SimpleNamespace(state="heat", attributes={})
    new = SimpleNamespace(state="heat", attributes={}, context=_context("run-1"))
    assert c._record_preference_observation(old, new, 66.0, 64.0) is False
    assert "Preference observation skipped: reason=caused by an automation" in caplog.text
    # A change made under no parent context, but right after an automation's call on the thermostat, is skipped too.
    c._automation_calls._calls.append((datetime.now(UTC), frozenset({CLIMATE})))
    plain = SimpleNamespace(state="heat", attributes={}, context=_context(None))
    caplog.clear()
    assert c._record_preference_observation(old, plain, 66.0, 64.0) is False
    assert "caused by an automation" in caplog.text
