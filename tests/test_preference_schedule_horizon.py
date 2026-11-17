"""A 24 h horizon from a Friday afternoon, workday to day off (fork Dunky13#1 phase 2; spec "Tests").

The chart band, the operation timeline and the v4 ``_ca`` line all read the learned schedule per moment of the
horizon. The week runs Fri (workday) -> Sat (day off) -> Sun (a workday by mapping), so the horizon's two nights are
two different day classes with different learned shifts, and every consumer must use both.
"""

from __future__ import annotations

from datetime import time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.climate_advisor import coordinator as coord_mod
from custom_components.climate_advisor import preference_learning as pl
from custom_components.climate_advisor import preference_schedule as ps
from custom_components.climate_advisor.automation import _in_sleep_window, select_comfort_band
from custom_components.climate_advisor.coordinator import (
    ClimateAdvisorCoordinator,
    _compute_target_band_schedule,
    _timeline_schedule,
)
from custom_components.climate_advisor.predictor_v4 import in_sleep_window as v4_in_sleep_window
from tests.test_preference_learning import CONFIG, _classification
from tests.test_preference_schedule import AMS, FRI, SAT, SUN, _at

START = _at(FRI, 15)
# Fri night wakes into a day off (weekend shifts), Sat night into Sunday, a mapped workday (weekday shifts).
VIEW = ps.ScheduleView(
    shifts={"weekday": {"wake": 0, "bed": 30}, "weekend": {"wake": 90, "bed": 120}},
    max_shift_min=180,
    workdays={FRI: True, SAT: False, SUN: True},
)
CFG = {**CONFIG, "wake_time": "06:30", "sleep_time": "22:30", "temp_unit": "fahrenheit"}
FRI_NIGHT = (time(8, 0), time(0, 30))  # (wake Sat, bed Fri): weekend wake +90, weekend bed +120
SAT_NIGHT = (time(6, 30), time(23, 0))  # (wake Sun, bed Sat): weekday wake +0, weekday bed +30


def _owner() -> SimpleNamespace:
    view = pl.PreferenceView(schedule=VIEW)
    return SimpleNamespace(config=CFG, preference_view=lambda: view)


# The nights as the user lives them, written out independently of the seam under test: asleep from the learned bed to
# the learned wake. Fri night 00:30 -> 08:00 (weekend shifts), Sat night 23:00 -> Sun 06:30 (weekday shifts).
NIGHTS = ((_at(SAT, 0, 30), _at(SAT, 8, 0)), (_at(SAT, 23, 0), _at(SUN, 6, 30)))
HORIZONS = {
    "friday afternoon": (START, 24),  # one night inside the horizon
    "saturday morning": (_at(SAT, 6), 30),  # two different nights: 23:00-00:30 and 06:30-08:00 differ per reader
}


def _asleep(ts):
    return any(bed <= ts < wake for bed, wake in NIGHTS)


@pytest.mark.parametrize("horizon", HORIZONS)
def test_the_chart_band_follows_the_learned_nights_across_the_horizon(horizon):
    start, count = HORIZONS[horizon]
    view = pl.PreferenceView(
        {"cold|evening|weekday|-": 2.0, "cold|night|weekend|-": 1.5, "cold|morning|weekend|-": -1.0},
        schedule=VIEW,
    )
    c = _classification()
    hours = [start + timedelta(hours=h) for h in range(count)]
    with patch.object(coord_mod.dt_util, "as_local", side_effect=lambda x: x):
        chart = _compute_target_band_schedule(hours, CFG, "home", start, classification=c, preference_view=view)
    ramps = [(bed, bed + timedelta(hours=1)) for bed, _ in NIGHTS] + [(w, w + timedelta(hours=2)) for _, w in NIGHTS]
    for ts, row in zip(hours, chart, strict=True):
        if any(a <= ts < b for a, b in ramps):  # the chart interpolates the ramps the engine's step does not have
            continue
        written = select_comfort_band(
            c,
            CFG,
            occupancy_mode="home",
            in_sleep_window=_asleep(ts),
            aggressive_savings=False,
            at=ts,
            preferences=view,
        )
        assert row["lower"] == pytest.approx(written.floor), ts
        assert row["upper"] == pytest.approx(written.ceiling), ts
    assert len({row["lower"] for row in chart}) >= 3  # the offsets and the sleep band all reached the chart


def test_the_timeline_follows_the_class_of_the_night_each_instant_belongs_to():
    from custom_components.climate_advisor.operation_timeline import build_segments, build_timeline, timeline_end
    from tests.timeline_fixtures import make_snapshot

    learned = _timeline_schedule(_owner())
    assert learned["schedule_at"](START) == FRI_NIGHT  # Friday afternoon: tonight, waking into the day off
    assert learned["schedule_at"](_at(SAT, 9)) == FRI_NIGHT  # the morning after: the same night
    assert learned["schedule_at"](_at(SAT, 13)) == SAT_NIGHT  # Saturday afternoon: the night waking into a workday
    assert learned["instants_on"](FRI, AMS) == (_at(FRI, 6, 30), _at(SAT, 0, 30))  # Friday's wake is a weekday's
    assert learned["instants_on"](SAT, AMS) == (_at(SAT, 8, 0), _at(SAT, 23, 0))
    assert learned["instants_on"](SUN, AMS)[0] == _at(SUN, 6, 30)

    snap = make_snapshot(now=_at(SAT, 6), **learned)
    segments = build_segments(snap, timeline_end(snap))
    starts = {seg.start for seg in segments}
    assert {_at(SAT, 8), _at(SAT, 23), _at(SUN, 6, 30)} <= starts  # the learned instants cut the segments
    covered = [seg for seg in segments if seg.start < _at(SUN, 12)]  # the nights written out above
    for seg in covered:
        assert seg.asleep is _asleep(seg.start), seg.start
    assert any(seg.asleep for seg in covered) and any(not seg.asleep for seg in covered)
    band_starts = {row[0] for row in build_timeline(snap).band}
    assert {_at(SAT, 8), _at(SAT, 23), _at(SUN, 6, 30)} <= band_starts


@pytest.mark.parametrize("horizon", HORIZONS)
def test_the_v4_ca_line_reads_the_pair_of_each_instant(horizon):
    from custom_components.climate_advisor.predictor_v4 import _pair_at

    start, count = HORIZONS[horizon]
    ca = ClimateAdvisorCoordinator._v4_ca_schedule(_owner())["ca_schedule"]
    assert (ca(START).wake, ca(START).bed) == ("08:00", "00:30")
    assert (ca(_at(SAT, 13)).wake, ca(_at(SAT, 13)).bed) == ("06:30", "23:00")
    # The loop predict() runs per step: the window test over the pair at that instant, not one pair for the horizon.
    for ts in (start + timedelta(minutes=30 * k) for k in range(count * 2)):
        assert v4_in_sleep_window(ts, *_pair_at(ts, "22:30", "06:30", ca)) is _asleep(ts), ts
        assert _in_sleep_window(ts, pl.config_for(CFG, _owner().preference_view(), ts)) is _asleep(ts), ts


def test_the_fallback_edges_before_a_classification_use_the_learned_night():
    from custom_components.climate_advisor.automation import fallback_edges_without_classification

    view = pl.PreferenceView(schedule=VIEW)
    at = _at(FRI, 23, 15)  # configured: asleep (after 22:30); learned bed 00:30: still awake
    assert fallback_edges_without_classification(at, CFG, view) == (CFG["comfort_heat"], CFG["comfort_cool"])
    assert fallback_edges_without_classification(at, CFG) == (CFG["sleep_heat"], CFG["sleep_cool"])
    owner = _owner()
    owner.current_classification = None
    with patch.object(coord_mod.dt_util, "now", return_value=at):
        edges = ClimateAdvisorCoordinator._resolve_active_comfort_band(owner)
    assert edges == (CFG["comfort_heat"], CFG["comfort_cool"])  # the coordinator call site hands over its view
