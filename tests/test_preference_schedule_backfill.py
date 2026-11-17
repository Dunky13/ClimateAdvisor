"""Learned wake and bed times, phase 2: history backfill, accept and discard of the seed's schedule rows (fork #1).

Occupant outcome: "Analyse history" also reads the wake and bed signal entities, drops what an automation does at the
same clock time every day, and proposes a shift per day class. Accept applies the chosen rows at once and re-arms the
timers; Discard drops the candidates.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.api import ClimateAdvisorPreferenceSeedView
from tests.test_preference_backfill_coordinator import (
    NOW,
    _backfill,
    _coordinator,
    _history,
    _Recorder,
    _request,
)
from tools.sim_harness._loop import run_coro
from tools.sim_harness.fake_hass import FakeState

LIGHT = "light.bedroom"
TV_LED = "switch.tv_led"
WORKDAY = "binary_sensor.workday"


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder(_history())
    module = rec.module()
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", module)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder.history", module.history)
    return rec


def _signal(at, state):
    return FakeState(state=state, last_changed=at, last_updated=at)


def _with_signals(rec, wake_delays=(40, 52, 28, 61, 35, 47)):
    """Six mornings the light comes on 28 to 61 minutes after 06:30; the TV LED automation goes off at 23:59:00."""
    start = NOW - timedelta(days=7)
    light = [_signal(start, "off")]
    tv = [_signal(start, "on")]
    for d, delay in zip(range(6, 0, -1), wake_delays, strict=True):
        day = (NOW - timedelta(days=d)).replace(hour=6, minute=30, second=0, microsecond=0)
        light += [_signal(day + timedelta(minutes=delay), "on"), _signal(day + timedelta(minutes=delay + 30), "off")]
        tv += [_signal(day.replace(hour=22), "on"), _signal(day.replace(hour=23, minute=59), "off")]
    rec.data[LIGHT] = light
    rec.data[TV_LED] = tv


def _seed(co):
    return co.learning._state.preference_seed


def test_the_backfill_reads_the_signal_entities_and_proposes_a_shift_per_class(recorder):
    _with_signals(recorder)
    co, sch = _coordinator(wake_signal_entities=[LIGHT], bed_signal_entities=[TV_LED])
    _backfill(co, sch)
    assert {LIGHT, TV_LED} <= set(recorder.calls[0]["entities"])
    section = _seed(co)["schedule"]
    assert [(c["date"], c["kind"], c["delta_min"], c["source"]) for c in section["candidates"]] == [
        ("2026-11-10", "wake", 40, "history"),
        ("2026-11-11", "wake", 52, "history"),
        ("2026-11-12", "wake", 28, "history"),
        ("2026-11-13", "wake", 61, "history"),
        ("2026-11-14", "wake", 35, "history"),
        ("2026-11-15", "wake", 47, "history"),
    ]
    # The TV LED goes off at 23:59:00 six nights running: an automation, filtered; no bed candidate.
    assert section["counts"] == {"transitions": 12, "schedule": 6, "outside": 0}
    rows = section["rows"]
    assert (rows["weekday|wake"]["n"], rows["weekday|wake"]["new_min"], rows["weekday|wake"]["enabled"]) == (
        4,
        45,
        True,
    )
    assert rows["weekend|wake"]["enabled"] is False and rows["weekend|wake"]["new_min"] is None
    assert co.learning.schedule_shifts()["weekday"]["wake"] == 0, "nothing applies before Accept"


def test_without_signal_entities_the_seed_has_no_schedule_section(recorder):
    co, sch = _coordinator()
    _backfill(co, sch)
    assert "schedule" not in _seed(co)


def test_every_workday_of_the_window_resolves_on_the_loop_before_the_executor_job(recorder):
    _with_signals(recorder)
    co, sch = _coordinator(wake_signal_entities=[LIGHT], workday_entity=WORKDAY)
    real = co.async_resolve_workday_dates

    async def spy(days):  # logged into the recorder's call list: the order is what is under test
        recorder.calls.append({"workdays": (threading.current_thread() is threading.main_thread(), days[0], days[-1])})
        return await real(days)

    co.async_resolve_workday_dates = spy
    _backfill(co, sch, days=7)
    on_loop, first, last = recorder.calls[0]["workdays"]
    assert on_loop
    assert (first, last) == ((NOW - timedelta(days=7)).date() - timedelta(days=1), NOW.date() + timedelta(days=2))
    assert "entities" in recorder.calls[1], "the recorder read followed the workday lookup"


def test_accept_applies_the_selected_schedule_rows_and_rearms_the_timers(recorder):
    _with_signals(recorder)
    co, sch = _coordinator(wake_signal_entities=[LIGHT], schedule_max_shift_min=30)
    _backfill(co, sch)
    co.async_refresh_schedule = AsyncMock()
    with sch.installed():
        result = run_coro(co.async_accept_preference_seed([], ["weekday|wake", "weekend|wake"]))
    assert result["schedule"] == ["weekday|wake"]  # weekend has 2 candidates: disabled
    assert co.learning.schedule_shifts()["weekday"]["wake"] == 30  # 40 clamped to the cap, not the weekly step
    assert len(co.learning.schedule_candidates()) == 4
    co.async_refresh_schedule.assert_awaited()


def test_discard_drops_the_candidates_and_rearms(recorder):
    _with_signals(recorder)
    co, sch = _coordinator(wake_signal_entities=[LIGHT])
    _backfill(co, sch)
    co.async_refresh_schedule = AsyncMock()
    with sch.installed():
        assert run_coro(co.async_discard_preference_seed()) is True
    assert co.learning.schedule_candidates() == [] and _seed(co) is None
    co.async_refresh_schedule.assert_awaited_once()


@pytest.mark.parametrize("rows", ["all", [1], {"a": 1}])
def test_the_seed_view_rejects_malformed_schedule_rows(rows):
    resp = asyncio.run(
        ClimateAdvisorPreferenceSeedView().post(_request(MagicMock(), {"action": "accept", "schedule_rows": rows}))
    )
    assert resp.status == 400


def test_the_weekly_update_moves_the_shift_and_the_end_of_day_rearms_the_timers(recorder):
    co, sch = _coordinator(schedule_max_shift_min=90, schedule_weekly_step_min=15)
    for i in range(5):
        day = (NOW - timedelta(days=5 - i)).date().isoformat()
        co.learning.add_schedule_candidate(
            {"date": day, "class": "weekday", "kind": "wake", "delta_min": 60}, "sensor", today=NOW.date()
        )
    co.async_refresh_schedule = AsyncMock()
    co._executor_job = AsyncMock()
    co._today_record = None
    with sch.installed():
        run_coro(co._async_end_of_day(NOW))
    assert co.learning.schedule_shifts()["weekday"]["wake"] == 15
    co.async_refresh_schedule.assert_awaited_once()
