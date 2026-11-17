"""Setpoint preference learning, phase 1 (fork Dunky13#1; docs/preference-learning-spec.md).

Pure module (buckets, episodes, weekly update), the single seam (``select_comfort_band``), the learning-engine
state and the disabled-means-unchanged guarantee.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.climate_advisor import preference_learning as pl
from custom_components.climate_advisor.automation import (
    awake_comfort_edges,
    band_resolver_for,
    compute_bedtime_setback,
    resolve_active_band,
    select_comfort_band,
)
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.setpoint_combinations import MIN_HEAT_COOL_GAP_F

WAKE, SLEEP = "06:30", "22:30"


def _at(y: int, m: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=UTC)


# 2026-11-16 is a Monday.
MON = (2026, 11, 16)
FRI = (2026, 11, 20)
SAT = (2026, 11, 21)
SUN = (2026, 11, 22)


def _key(day: tuple[int, int, int], h: int, mi: int = 0, *, asleep: bool = False, wake: str = WAKE, sleep: str = SLEEP):
    return pl.bucket_key(_at(*day, h, mi), "cold", wake, sleep, in_sleep_window=asleep)


# ── buckets ─────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hour", "part"),
    [(7, "morning"), (11, "morning"), (12, "afternoon"), (16, "afternoon"), (17, "evening"), (22, "evening")],
)
def test_awake_parts_follow_the_clock(hour, part):
    assert _key(MON, hour) == f"cold|{part}|weekday"


def test_weekend_class_uses_calendar_day():
    assert _key(SAT, 9) == "cold|morning|weekend"
    assert _key(SUN, 20) == "cold|evening|weekend"


def test_a_night_belongs_to_the_day_it_wakes_into():
    # Friday 23:00 wakes Saturday (weekend); Sunday 23:00 wakes Monday (weekday); the small hours are the same night.
    assert _key(FRI, 23, asleep=True) == "cold|night|weekend"
    assert _key(SUN, 23, asleep=True) == "cold|night|weekday"
    assert _key(SAT, 3, asleep=True) == "cold|night|weekend"
    assert _key(MON, 3, asleep=True) == "cold|night|weekday"


def test_sleep_window_flag_decides_night_not_the_clock():
    assert _key(MON, 23, asleep=True).split("|")[1] == "night"
    assert _key(MON, 23, asleep=False).split("|")[1] == "evening"


def test_wake_at_or_after_noon_leaves_the_morning_empty():
    parts = {_key(MON, h, wake="13:00", sleep="02:00").split("|")[1] for h in range(13, 24)}
    assert "morning" not in parts


def test_night_owl_small_hours_awake_are_the_previous_evening():
    # Bed at 01:00, wake 09:00: 00:30 Saturday is awake, and still Friday evening (weekday class stays Friday's).
    key = pl.bucket_key(_at(*SAT, 0, 30), "cold", "09:00", "01:00", in_sleep_window=False)
    assert key == "cold|evening|weekday"


def test_bucket_parts_roundtrip():
    assert pl.bucket_parts("hot|afternoon|weekend") == ("hot", "afternoon", "weekend")


# ── view ────────────────────────────────────────────────────────────────────────────────────────────────


def test_view_clamps_to_the_current_cap_on_read():
    view = pl.PreferenceView({"cold|evening|weekday": 3.6}, max_offset_f=1.8)
    assert view.offset_for("cold|evening|weekday") == 1.8
    assert view.offset_for("cold|morning|weekday") == 0.0


def test_view_offset_at_returns_bucket():
    view = pl.PreferenceView({"cold|evening|weekday": 1.0})
    offset, bucket = view.offset_at(
        _at(*MON, 18), "cold", {"wake_time": WAKE, "sleep_time": SLEEP}, in_sleep_window=False
    )
    # Phase 3: the bucket is the four-part key (band "-" with no humidity); the three-part offset still applies.
    assert (offset, bucket) == (1.0, "cold|evening|weekday|-")


# ── episodes ────────────────────────────────────────────────────────────────────────────────────────────


def _start(at=None, **kw):
    args = {"old_f": 70.0, "new_f": 72.0, "bucket": "cold|evening|weekday", "base_f": 70.0, "target_f": 70.0}
    args.update(kw)
    return pl.extend_episode(None, at=at or _at(*MON, 18), **args)


def test_changes_within_15_minutes_extend_one_episode_and_final_counts():
    ep, closed = _start()
    assert closed is None
    ep, closed = pl.extend_episode(
        ep,
        at=_at(*MON, 18, 10),
        old_f=72.0,
        new_f=73.0,
        bucket="cold|evening|weekday",
        base_f=70.0,
        target_f=70.0,
    )
    assert closed is None
    obs = pl.close_episode(ep)
    assert obs is not None
    assert obs["delta_f"] == 3.0
    assert obs["bucket"] == "cold|evening|weekday"
    assert obs["date"] == "2026-11-16"
    assert obs["time"] == "18:10"


def test_a_change_after_the_gap_closes_the_previous_episode():
    ep, _ = _start()
    ep2, closed = pl.extend_episode(
        ep,
        at=_at(*MON, 19),
        old_f=72.0,
        new_f=74.0,
        bucket="cold|evening|weekday",
        base_f=70.0,
        target_f=70.0,
    )
    assert closed is not None
    assert closed["delta_f"] == 2.0
    assert ep2["final_f"] == 74.0


def test_the_bucket_is_keyed_at_episode_start():
    ep, _ = _start()
    ep, _ = pl.extend_episode(
        ep,
        at=_at(*MON, 18, 5),
        old_f=72.0,
        new_f=73.0,
        bucket="cold|night|weekday",
        base_f=60.0,
        target_f=60.0,
    )
    assert ep["bucket"] == "cold|evening|weekday"
    assert ep["base_f"] == 70.0


def test_undoing_the_change_discards_the_episode():
    ep, _ = _start(old_f=70.5, new_f=72.0, target_f=70.5)
    ep, _ = pl.extend_episode(
        ep, at=_at(*MON, 18, 3), old_f=72.0, new_f=70.8, bucket="x|y|z", base_f=70.0, target_f=70.5
    )
    assert pl.close_episode(ep) is None


def test_delta_is_measured_against_the_base_not_the_offset_target():
    # The floor already carries +1.0 (target 71, base 70); the occupant lands on 72: the preference is +2.0 over base.
    ep, _ = _start(old_f=71.0, new_f=72.0, base_f=70.0, target_f=71.0)
    assert pl.close_episode(ep)["delta_f"] == 2.0


def test_episode_expiry():
    ep, _ = _start()
    assert not pl.episode_expired(ep, _at(*MON, 18, 15))
    assert pl.episode_expired(ep, _at(*MON, 18, 16))


def test_trim_keeps_400_days_and_2000_entries():
    today = date(2026, 11, 16)
    old = {"date": (today - timedelta(days=401)).isoformat(), "bucket": "cold|evening|weekend", "delta_f": 1.0}
    new = [{"date": today.isoformat(), "bucket": "cold|evening|weekend", "delta_f": float(i)} for i in range(2100)]
    kept = pl.trim_observations([old, *new], today)
    assert old not in kept
    assert len(kept) == 2000
    assert kept[-1]["delta_f"] == 2099.0


# ── weekly update ───────────────────────────────────────────────────────────────────────────────────────


def _obs(bucket: str, *deltas: float) -> list[dict]:
    return [{"date": "2026-11-01", "time": "18:00", "bucket": bucket, "delta_f": d} for d in deltas]


TODAY = date(2026, 11, 16)
B = "cold|evening|weekday"


def test_update_due_after_seven_days_or_never():
    assert pl.update_due(None, TODAY)
    assert pl.update_due("2026-11-09", TODAY)
    assert not pl.update_due("2026-11-10", TODAY)
    assert pl.update_due("garbage", TODAY)


def test_update_moves_toward_the_median_by_at_most_one_step():
    offsets, changes = pl.weekly_update({}, _obs(B, 3.0, 3.0, 3.0), max_offset_f=3.6, step_f=0.9, today=TODAY)
    assert offsets[B]["offset_f"] == 0.9
    assert offsets[B]["level"] == pl.LEVEL_BUCKET
    assert offsets[B]["n"] == 3
    assert changes[0]["new_f"] == 0.9
    assert changes[0]["old_f"] == 0.0


def test_update_target_is_the_median_not_the_mean():
    offsets, _ = pl.weekly_update({}, _obs(B, 1.0, 1.0, 9.0), max_offset_f=3.6, step_f=5.0, today=TODAY)
    assert offsets[B]["offset_f"] == 1.0


def test_update_clamps_target_and_result_to_the_max():
    offsets, _ = pl.weekly_update({}, _obs(B, 9.0, 9.0, 9.0), max_offset_f=2.0, step_f=5.0, today=TODAY)
    assert offsets[B]["offset_f"] == 2.0
    start = {B: {"offset_f": 1.8}}
    offsets, _ = pl.weekly_update(start, _obs(B, -9.0, -9.0, -9.0), max_offset_f=2.0, step_f=1.5, today=TODAY)
    assert offsets[B]["offset_f"] == 0.3


def test_update_needs_three_observations():
    offsets, changes = pl.weekly_update({}, _obs(B, 3.0, 3.0), max_offset_f=3.6, step_f=0.9, today=TODAY)
    assert offsets == {}
    assert changes == []


def test_update_falls_back_to_the_day_type_part_level():
    sibling = "cold|evening|weekend"
    observations = _obs(B, 2.0) + _obs(sibling, 2.0, 2.0)
    offsets, changes = pl.weekly_update({}, observations, max_offset_f=3.6, step_f=0.9, today=TODAY)
    assert offsets[B]["level"] == pl.LEVEL_DAYTYPE_PART
    assert offsets[B]["n"] == 3
    assert offsets[sibling]["level"] == pl.LEVEL_DAYTYPE_PART
    # Phase 3: with only band "-" observations no "-" key mirrors the three-part key (one row, one history record).
    assert f"{B}|-" not in offsets
    assert {c["bucket"] for c in changes} == {B, sibling}


def test_update_uses_only_the_newest_twenty():
    observations = _obs(B, *([5.0] * 30 + [1.0] * 20))
    offsets, _ = pl.weekly_update({}, observations, max_offset_f=3.6, step_f=5.0, today=TODAY)
    assert offsets[B]["offset_f"] == 1.0
    assert offsets[B]["n"] == 20


def test_update_rounds_to_a_tenth():
    offsets, _ = pl.weekly_update({}, _obs(B, 0.33, 0.33, 0.33), max_offset_f=3.6, step_f=0.9, today=TODAY)
    assert offsets[B]["offset_f"] == 0.3


def test_unchanged_offset_is_not_a_change_record():
    start = {B: {"offset_f": 1.0}}
    _, changes = pl.weekly_update(start, _obs(B, 1.0, 1.0, 1.0), max_offset_f=3.6, step_f=0.9, today=TODAY)
    assert B not in {c["bucket"] for c in changes}


# ── the seam ────────────────────────────────────────────────────────────────────────────────────────────


def _classification(day_type: str = "cold", hvac_mode: str = "heat") -> DayClassification:
    c = object.__new__(DayClassification)
    c.__dict__.update(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=0,
        today_high=40,
        today_low=25,
        tomorrow_high=40,
        tomorrow_low=25,
        hvac_mode=hvac_mode,
        pre_condition=False,
        pre_condition_target=None,
        windows_recommended=False,
        window_open_time=None,
        window_close_time=None,
        setback_modifier=0.0,
        window_opportunity_morning=False,
        window_opportunity_evening=False,
    )
    return c


CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 78,
    "sleep_heat": 66,
    "sleep_cool": 78,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": WAKE,
    "sleep_time": SLEEP,
}
VIEW = pl.PreferenceView(
    {
        "cold|evening|weekday": 2.0,
        "cold|night|weekday": 1.5,
        "cold|morning|weekday": -1.0,
        "cold|evening|weekend": 9.0,
    },
    max_offset_f=3.6,
)


def _band(occupancy="home", asleep=False, at=None, preferences=VIEW, savings=False, c=None):
    return select_comfort_band(
        c or _classification(),
        CONFIG,
        occupancy_mode=occupancy,
        in_sleep_window=asleep,
        aggressive_savings=savings,
        at=at or _at(*MON, 18),
        preferences=preferences,
    )


def test_offset_reaches_the_awake_home_floor_only():
    band = _band()
    assert band.floor == 72.0
    assert band.ceiling == 78.0


def test_offset_reaches_the_sleep_floor_only():
    band = _band(asleep=True, at=_at(*MON, 23))
    assert band.floor == 67.5
    assert band.ceiling == 78.0


def test_negative_offset_lowers_the_floor():
    assert _band(at=_at(*MON, 9)).floor == 69.0


def test_guest_gets_the_awake_offset():
    assert _band(occupancy="guest").floor == 72.0


@pytest.mark.parametrize("occupancy", ["away", "vacation"])
def test_away_and_vacation_never_get_an_offset(occupancy):
    assert _band(occupancy=occupancy) == _band(occupancy=occupancy, preferences=None)


def test_the_offset_never_moves_the_ceiling_or_the_cooling_day():
    cool_day = _classification("hot", hvac_mode="cool")
    view = pl.PreferenceView({"hot|evening|weekday": 3.0})
    with_pref = _band(c=cool_day, preferences=view)
    without = _band(c=cool_day, preferences=None)
    assert with_pref.ceiling == without.ceiling
    assert with_pref.floor == min(without.floor + 3.0, without.ceiling - MIN_HEAT_COOL_GAP_F)


def test_floor_keeps_the_minimum_heat_cool_gap_below_the_ceiling():
    view = pl.PreferenceView({"cold|evening|weekday": 5.4}, max_offset_f=5.4)
    cfg = {**CONFIG, "comfort_heat": 74}
    band = select_comfort_band(
        _classification(),
        cfg,
        occupancy_mode="home",
        in_sleep_window=False,
        aggressive_savings=False,
        at=_at(*MON, 18),
        preferences=view,
    )
    # configured gap is exactly the 4F minimum (74/78): the learned offset adds nothing
    assert (band.floor, band.ceiling) == (74.0, 78.0)
    wide = select_comfort_band(
        _classification(),
        CONFIG,
        occupancy_mode="home",
        in_sleep_window=False,
        aggressive_savings=False,
        at=_at(*MON, 18),
        preferences=view,
    )
    assert wide.ceiling - wide.floor == MIN_HEAT_COOL_GAP_F  # 70 + 5.4 would pass 74: held 4F under 78


def test_weekend_bucket_is_its_own_bucket():
    # 2026-11-21 is a Saturday: the 9.0 stored offset is clamped to the cap of 3.6.
    assert _band(at=_at(*SAT, 18)).floor == 73.6


def test_prefer_savings_stacks_with_the_offset():
    assert _band(savings=True).floor == _band(savings=True, preferences=None).floor + 2.0


def test_disabled_changes_nothing_across_instants_occupancy_and_day_types():
    """Feature off (``preferences=None`` and no instant) yields exactly the upstream band everywhere."""
    for day_type, occupancy, asleep, savings, day in itertools.product(
        ("hot", "warm", "mild", "cool", "cold"),
        ("home", "guest", "away", "vacation"),
        (False, True),
        (False, True),
        (MON, SAT, SUN),
    ):
        c = _classification(day_type, "cool" if day_type == "hot" else "heat")
        for hour in (0, 6, 9, 13, 18, 23):
            at = _at(*day, hour)
            kwargs = {
                "occupancy_mode": occupancy,
                "in_sleep_window": asleep,
                "aggressive_savings": savings,
            }
            upstream = select_comfort_band(c, CONFIG, **kwargs)
            assert select_comfort_band(c, CONFIG, **kwargs, at=at, preferences=None) == upstream
            empty = pl.PreferenceView({}, max_offset_f=3.6)
            assert select_comfort_band(c, CONFIG, **kwargs, at=at, preferences=empty) == upstream


def test_a_non_view_object_is_ignored_like_the_feature_being_off():
    class Stub:
        pass

    c = _classification()
    kwargs = {"occupancy_mode": "home", "in_sleep_window": False, "aggressive_savings": False}
    assert select_comfort_band(c, CONFIG, **kwargs, at=_at(*MON, 18), preferences=Stub()) == select_comfort_band(
        c, CONFIG, **kwargs
    )


def test_resolve_active_band_and_band_resolver_pass_the_view_through():
    c = _classification()
    now = _at(*MON, 18)
    assert resolve_active_band(c, CONFIG, occupancy_mode="home", now=now, preferences=VIEW).floor == 72.0
    assert resolve_active_band(c, CONFIG, occupancy_mode="home", now=now).floor == 70.0
    resolver = band_resolver_for(c, CONFIG, VIEW)
    assert callable(resolver)


def test_awake_comfort_edges_offset_is_capped_at_the_ceiling():
    assert awake_comfort_edges(CONFIG, aggressive_savings=False) == (70.0, 78.0)
    assert awake_comfort_edges(CONFIG, aggressive_savings=False, preference_offset_f=2.0) == (72.0, 78.0)
    assert awake_comfort_edges(CONFIG, aggressive_savings=False, preference_offset_f=9.0) == (74.0, 78.0)


def test_bedtime_setback_carries_the_night_offset():
    c = _classification()
    base = compute_bedtime_setback(CONFIG, None, c)
    shifted = compute_bedtime_setback(CONFIG, None, c, preference_offset_f=1.5)
    assert shifted == pytest.approx(base + 1.5)


# ── learning engine state ───────────────────────────────────────────────────────────────────────────────


def _engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _change(engine, at, new_f, *, old_f=70.0, base_f=70.0, target_f=70.0, bucket=B):
    engine.record_preference_change(at=at, old_f=old_f, new_f=new_f, bucket=bucket, base_f=base_f, target_f=target_f)


def test_episode_becomes_an_observation_lazily(tmp_path):
    engine = _engine(tmp_path)
    _change(engine, _at(*MON, 18), 73.0)
    assert engine.preference_summary()["observation_count"] == 0
    assert not engine.flush_preference_episode(_at(*MON, 18, 10))
    assert engine.flush_preference_episode(_at(*MON, 18, 20))
    assert engine.preference_summary()["observation_count"] == 1


def test_undone_episode_leaves_no_observation(tmp_path):
    engine = _engine(tmp_path)
    _change(engine, _at(*MON, 18), 72.0)
    _change(engine, _at(*MON, 18, 2), 70.2, old_f=72.0)
    assert engine.flush_preference_episode(_at(*MON, 19))
    assert engine.preference_summary()["observation_count"] == 0


def test_weekly_update_state_roundtrips_through_disk(tmp_path):
    engine = _engine(tmp_path)
    for day in range(3):
        _change(engine, _at(2026, 11, 2 + day, 18), 73.0)
        engine.flush_preference_episode(_at(2026, 11, 2 + day, 19))
    changes = engine.run_preference_update(TODAY, max_offset_f=3.6, step_f=0.9, unit="fahrenheit")
    assert B in [c["bucket"] for c in changes]
    assert engine.run_preference_update(TODAY, max_offset_f=3.6, step_f=0.9, unit="fahrenheit") is None  # not due again
    engine.save_state()

    reloaded = _engine(tmp_path)
    assert reloaded.preference_offsets()[B] == 0.9
    assert reloaded.preference_summary()["last_update"] == TODAY.isoformat()
    assert reloaded.preference_summary()["observation_count"] == 3


def test_load_state_drops_malformed_preference_fields_without_losing_the_file(tmp_path):
    engine = _engine(tmp_path)
    engine._state.preference_offsets = {
        "bad-key": {"offset_f": 1.0},
        B: {"offset_f": "x"},
        "cold|evening|weekend": {"offset_f": 1.0},
    }
    engine._state.preference_observations = [
        {"date": "2026-11-01", "bucket": "cold|evening|weekend", "delta_f": True},
        "junk",
    ]
    engine._state.preference_history = "nope"
    engine._state.preference_last_update = 5
    engine._state.preference_open_episode = {"start": "not a date"}
    engine.save_state()

    reloaded = _engine(tmp_path)
    assert reloaded.preference_offsets() == {"cold|evening|weekend": 1.0}
    assert reloaded.preference_summary()["observation_count"] == 0
    assert reloaded.preference_summary()["last_update"] is None
    assert reloaded._state.preference_open_episode is None
    assert reloaded._state.preference_history == []


def test_open_episode_survives_a_restart(tmp_path):
    engine = _engine(tmp_path)
    _change(engine, _at(*MON, 18), 73.0)
    engine.save_state()
    reloaded = _engine(tmp_path)
    assert reloaded.flush_preference_episode(_at(*MON, 19))
    assert reloaded.preference_summary()["observation_count"] == 1


def test_reset_preferences_clears_only_the_preference_fields(tmp_path):
    engine = _engine(tmp_path)
    _change(engine, _at(*MON, 18), 73.0)
    engine.flush_preference_episode(_at(*MON, 19))
    engine._state.preference_offsets = {B: {"offset_f": 1.0, "n": 3, "level": "bucket", "updated": "2026-11-16"}}
    engine._state.dismissed_suggestions = ["keep_me"]
    engine.reset("preferences")
    assert engine.preference_offsets() == {}
    assert engine.preference_summary()["observation_count"] == 0
    assert engine._state.dismissed_suggestions == ["keep_me"]


def test_accepting_frequent_overrides_enables_preference_learning(tmp_path):
    engine = _engine(tmp_path)
    assert engine.accept_suggestion("frequent_overrides") == {"preference_learning_enabled": True}


# ── the engine's own call sites: the setpoint actually written carries the offset ──────────────────────


def _written_setpoints(method: str, now: datetime, provider) -> list[float]:
    """The comfort-band floors the engine arms (``_apply_comfort_band``, the one write funnel) when ``method`` runs."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from tests.test_bedtime_setback import _make_engine

    engine = _make_engine(
        {"comfort_cool": 78, "sleep_heat": 66, "sleep_cool": 78, "wake_time": WAKE, "sleep_time": SLEEP}
    )
    engine._preference_view_provider = provider
    engine._current_classification = _classification()
    engine.set_occupancy_mode("home")
    with (
        patch("custom_components.climate_advisor.automation.dt_util.now", return_value=now),
        patch.object(engine, "_apply_comfort_band", new_callable=AsyncMock) as apply_band,
    ):
        if method == "apply_classification":
            asyncio.run(engine.apply_classification(engine._current_classification))
        else:
            asyncio.run(engine.handle_bedtime())
    return [c.args[0].floor for c in apply_band.call_args_list]  # the floor the active-edge write sends (heat day)


@pytest.mark.parametrize(
    ("method", "now", "base", "offset"),
    [
        ("apply_classification", _at(*MON, 18), 70.0, 2.0),
        ("handle_bedtime", _at(*MON, 22, 30), 66.0, 1.5),
    ],
)
def test_the_written_setpoint_carries_the_learned_offset_and_only_when_enabled(method, now, base, offset):
    view = pl.PreferenceView({"cold|evening|weekday": 2.0, "cold|night|weekday": 1.5})
    assert _written_setpoints(method, now, None) == [base]
    assert _written_setpoints(method, now, lambda: view) == [base + offset]


# ── schedule-edge exclusion, discard, corrupt files (review fixes) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("hour", "minute", "wake", "sleep", "near"),
    [
        (5, 30, "06:30", "22:30", True),  # an hour before wake
        (5, 29, "06:30", "22:30", False),
        (21, 45, "06:30", "22:30", True),  # before bedtime
        (12, 0, "06:30", "22:30", False),
        (23, 30, "00:15", "22:30", True),  # wake just after midnight: the hour before wraps over midnight
        (0, 20, "00:15", "22:30", False),  # after wake
        (22, 0, "06:30", "00:30", False),  # bedtime past midnight, 2.5 h away
        (23, 45, "06:30", "00:30", True),
    ],
)
def test_near_schedule_edge(hour, minute, wake, sleep, near):
    assert pl.near_schedule_edge(_at(*MON, hour, minute), wake, sleep) is near


def test_an_excluded_episode_start_is_not_recorded(tmp_path):
    engine = _engine(tmp_path)
    changed = engine.record_preference_change(
        at=_at(*MON, 6, 0), old_f=70.0, new_f=73.0, bucket=B, base_f=70.0, target_f=70.0, excluded=True
    )
    assert changed is False
    assert engine._state.preference_open_episode is None


def test_disabled_discards_the_open_episode_instead_of_recording_it(tmp_path):
    engine = _engine(tmp_path)
    _change(engine, _at(*MON, 18), 73.0)
    assert engine.discard_preference_episode() is True
    assert engine.flush_preference_episode(_at(*MON, 19)) is False
    assert engine.preference_summary()["observation_count"] == 0
    assert engine.discard_preference_episode() is False


def test_corrupt_episode_fields_are_dropped_with_a_warning(tmp_path, caplog):
    engine = _engine(tmp_path)
    engine._state.preference_open_episode = {
        "start": "2026-11-16T18:00:00",  # naive
        "last": "2026-11-16T18:00:00+00:00",
        "bucket": "cold|evening|weekday",
        "base_f": 70.0,
        "target_f": 70.0,
        "old_f": 70.0,
        "final_f": float("nan"),
    }
    engine._state.preference_offsets = {B: {"offset_f": float("inf")}, "cold|nope|weekday": {"offset_f": 1.0}}
    engine.save_state()
    with caplog.at_level("WARNING"):
        reloaded = _engine(tmp_path)
    assert reloaded._state.preference_open_episode is None
    assert reloaded.preference_offsets() == {}
    assert caplog.text.count("Preference state entry dropped:") >= 3


def test_the_remote_offset_captured_at_episode_open_covers_every_change_in_it(tmp_path):
    # Thermostat reads 72 = armed 70 + 2 remote. The occupant sets 74; the remote tick then drops the offset to 0;
    # they go back to 72 three minutes later. That is an undo (72 - 2 = 70 = the target), not a +2.0 preference.
    engine = _engine(tmp_path)
    kw = {"bucket": B, "base_f": 70.0, "target_f": 70.0}
    engine.record_preference_change(at=_at(*MON, 18), old_f=72.0, new_f=74.0, remote_f=2.0, **kw)
    engine.record_preference_change(at=_at(*MON, 18, 3), old_f=74.0, new_f=72.0, remote_f=0.0, **kw)
    assert engine._state.preference_open_episode["remote_f"] == 2.0
    assert engine.flush_preference_episode(_at(*MON, 19)) is True
    assert engine.preference_summary()["observation_count"] == 0


def test_an_episode_saved_without_a_remote_offset_loads_with_zero(tmp_path):
    engine = _engine(tmp_path)
    engine._state.preference_open_episode = {
        "start": "2026-11-16T18:00:00+00:00",
        "last": "2026-11-16T18:00:00+00:00",
        "bucket": B,
        "base_f": 70.0,
        "target_f": 70.0,
        "old_f": 70.0,
        "final_f": 73.0,
    }
    engine.save_state()
    assert _engine(tmp_path)._state.preference_open_episode["remote_f"] == 0.0


def test_reset_preferences_clears_nothing_but_the_preference_fields_via_the_view(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.climate_advisor.api import ClimateAdvisorResetPreferencesView
    from custom_components.climate_advisor.const import DOMAIN
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    engine = _engine(tmp_path)
    engine._state.preference_offsets = {B: {"offset_f": 1.0, "n": 3, "level": "bucket", "updated": "2026-11-16"}}
    engine._state.dismissed_suggestions = ["keep_me"]
    coordinator = MagicMock()
    coordinator.learning = engine
    coordinator.async_refresh_schedule = AsyncMock()
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}

    async def _run_in_executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _run_in_executor
    coordinator.hass = hass
    coordinator.async_reset_learning = ClimateAdvisorCoordinator.async_reset_learning.__get__(coordinator)
    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {}
    resp = asyncio.run(ClimateAdvisorResetPreferencesView().post(request))

    assert resp.json_data["status"] == "ok"
    coordinator.async_refresh_schedule.assert_awaited_once()  # the timers follow the cleared shifts
    assert engine._state.preference_offsets == {}
    assert engine._state.dismissed_suggestions == ["keep_me"]
