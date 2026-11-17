"""Learned wake and bed times, phase 2: the pure core (fork Dunky13#1; docs/preference-learning-spec.md).

The effective-schedule seam (noon pivot, day class with a Workday mapping), candidate extraction, the +-2-minute
schedule filter, the weekly update and the seed review rows.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import preference_learning as pl
from custom_components.climate_advisor import preference_schedule as ps
from custom_components.climate_advisor.automation import _in_sleep_window
from custom_components.climate_advisor.predictor_v4 import in_sleep_window as v4_in_sleep_window

AMS = ZoneInfo("Europe/Amsterdam")
CFG = {"wake_time": "06:30:00", "sleep_time": "22:30:00"}
# 2026-11-20 is a Friday.
FRI, SAT, SUN, MON = date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22), date(2026, 11, 23)


def _view(weekday=(0, 0), weekend=(0, 0), cap=90, workdays=None):
    return ps.ScheduleView(
        shifts={"weekday": {"wake": weekday[0], "bed": weekday[1]}, "weekend": {"wake": weekend[0], "bed": weekend[1]}},
        max_shift_min=cap,
        workdays=workdays or {},
    )


def _at(day: date, h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=AMS)


# ── day class ───────────────────────────────────────────────────────────────────────────────────────────


def test_day_class_follows_the_weekday_rule_without_a_mapping():
    assert ps.day_class(FRI) == "weekday"
    assert ps.day_class(SAT) == "weekend"
    assert ps.day_class(SUN, {}) == "weekend"


def test_day_class_uses_the_workday_mapping_and_falls_back_for_a_missing_date():
    holiday = {date(2026, 11, 23): False, SAT: True}  # a holiday on Monday, a working Saturday
    assert ps.day_class(date(2026, 11, 23), holiday) == "weekend"
    assert ps.day_class(SAT, holiday) == "weekday"
    assert ps.day_class(date(2026, 11, 24), holiday) == "weekday"  # missing: the weekday rule


# ── effective times ─────────────────────────────────────────────────────────────────────────────────────


def test_nothing_learned_returns_the_configured_strings_verbatim():
    for view in (_view(), _view((35, 80), (90, 80), cap=0)):
        assert view.effective_times(CFG, FRI) == ("06:30:00", "22:30:00")
        assert view.schedule_at(CFG, _at(FRI, 13)) == ("22:30:00", "06:30:00")
        assert ps.config_at(view, CFG, _at(FRI, 13)) is CFG
    assert ps.config_at(None, CFG, _at(FRI, 13)) is CFG


def test_bedtime_uses_the_class_of_the_day_the_night_wakes_into():
    view = _view(weekday=(35, 0), weekend=(90, 80))
    # Friday morning: weekday wake; Friday evening: the night wakes into Saturday (weekend) bed shift.
    assert view.effective_times(CFG, FRI) == ("07:05", "23:50")
    # Sunday evening wakes into Monday (weekday): bed shift 0, so the configured time verbatim.
    assert view.effective_times(CFG, SUN) == ("08:00", "22:30:00")


def test_the_shift_is_clamped_to_the_current_cap():
    view = _view(weekday=(120, -120), cap=60)
    assert view.shift("weekday", "wake") == 60
    assert view.shift("weekday", "bed") == -60
    assert view.effective_times(CFG, MON)[0] == "07:30"


def test_noon_pivot_pairs_the_night_that_matters():
    view = _view(weekday=(35, 80), weekend=(90, 80))
    # Saturday 11:00: the night that ends this morning: Friday's bedtime (wakes into Saturday), Saturday's wake.
    assert view.schedule_at(CFG, _at(SAT, 11, 0)) == ("23:50", "08:00")
    # Saturday 12:00: the coming night: Saturday's bedtime (wakes into Sunday), Sunday's wake.
    assert view.schedule_at(CFG, _at(SAT, 12, 0)) == ("23:50", "08:00")
    # Friday 13:00: Friday's bedtime, Saturday's wake.
    assert view.schedule_at(CFG, _at(FRI, 13, 0)) == ("23:50", "08:00")
    # Friday 11:59: Thursday's bedtime (wakes into Friday), Friday's wake.
    assert view.schedule_at(CFG, _at(FRI, 11, 59)) == ("23:50", "07:05")
    # Sunday 13:00: Sunday's bedtime wakes into Monday (weekday, bed shift 80); Monday's wake is shifted 35.
    assert view.schedule_at(CFG, _at(SUN, 13, 0)) == ("23:50", "07:05")


def test_a_holiday_makes_the_day_before_a_weekend_night():
    holiday = {MON: False}
    view = _view(weekday=(35, 0), weekend=(90, 80), workdays=holiday)
    assert view.effective_times(CFG, SUN) == ("08:00", "23:50")  # Sunday night wakes into the holiday
    assert view.effective_times(CFG, MON)[0] == "08:00"  # the holiday itself wakes late


@pytest.mark.parametrize(
    ("bed", "after_midnight"),
    [("23:59", False), ("00:00", True), ("00:10", True)],
)
def test_the_sleep_window_branch_flips_at_midnight_and_both_implementations_agree(bed, after_midnight):
    """#1376: bedtime 00:00 or later does not wrap; automation and v4 stay pinned together on every instant."""
    config = {"wake_time": "06:30", "sleep_time": bed}
    assert (ps.clock_minutes(bed) < ps.clock_minutes("06:30")) is after_midnight
    day = datetime(2026, 11, 20, tzinfo=AMS)
    for hour in range(24):
        for minute in (0, 9, 10, 11, 30, 59):
            at = day.replace(hour=hour, minute=minute)
            assert _in_sleep_window(at, config) == v4_in_sleep_window(at, bed, "06:30")
    # The night in progress at 00:05 and at 12:30 the next afternoon (a learned bedtime of 00:10 is not asleep then).
    assert _in_sleep_window(day.replace(hour=12, minute=30), config) is False
    assert _in_sleep_window(day.replace(hour=3), config) is True


def test_a_learned_bedtime_past_midnight_flips_the_branch_through_the_seam():
    view = _view(weekday=(0, 100), weekend=(0, 100), cap=180)  # 22:30 + 100 min = 00:10
    pair_evening = view.schedule_at(CFG, _at(FRI, 23, 30))
    assert pair_evening == ("00:10", "06:30:00")
    assert _in_sleep_window(_at(FRI, 23, 30), ps.config_at(view, CFG, _at(FRI, 23, 30))) is False
    assert _in_sleep_window(_at(SAT, 0, 20), ps.config_at(view, CFG, _at(SAT, 0, 20))) is True
    assert _in_sleep_window(_at(SAT, 12, 30), ps.config_at(view, CFG, _at(SAT, 12, 30))) is False


def test_dst_night_wall_clock_times_hold():
    """2026-10-25 (Europe/Amsterdam) has 25 hours: the schedule is the same wall clock before and after the change."""
    view = _view(weekday=(0, 80), weekend=(0, 80))
    night = date(2026, 10, 24)  # Saturday evening, the night of the change
    _wake, bed = view.effective_times(CFG, night)
    assert bed == "23:50"
    assert _in_sleep_window(_at(night, 23, 55), ps.config_at(view, CFG, _at(night, 23, 55))) is True
    after = _at(date(2026, 10, 25), 5, 0)  # 05:00 CET, five hours after midnight in elapsed time: 6 h after 23:00 CEST
    assert _in_sleep_window(after, ps.config_at(view, CFG, after)) is True
    nxt = ps.next_times(view, CFG, _at(night, 12, 0))
    assert nxt["bed"] == _at(night, 23, 50)
    assert nxt["wake"] == _at(date(2026, 10, 25), 6, 30)


def test_next_times_moves_a_bedtime_past_midnight_to_the_right_calendar_day():
    view = _view(weekday=(0, 100), weekend=(0, 100), cap=180)  # bed 00:10
    nxt = ps.next_times(view, CFG, _at(FRI, 13, 0))
    assert nxt["bed"] == _at(SAT, 0, 10)
    assert nxt["wake"] == _at(SAT, 6, 30)
    # After the bedtime fired, the next one is a day later.
    assert ps.next_times(view, CFG, _at(SAT, 0, 10))["bed"] == _at(SUN, 0, 10)


def test_part_of_day_and_the_bucket_use_the_pair_and_the_workday_mapping():
    holiday = {SAT: True}  # Saturday worked: the night before it wakes into a weekday
    view = _view(workdays=holiday)
    at = _at(FRI, 23, 0)
    key = pl.bucket_key(at, "cold", "06:30", "22:30", in_sleep_window=True, workdays=view.workdays)
    assert key == "cold|night|weekday"
    assert pl.bucket_key(at, "cold", "06:30", "22:30", in_sleep_window=True) == "cold|night|weekend"


# ── candidates ──────────────────────────────────────────────────────────────────────────────────────────


def test_a_wake_candidate_is_the_distance_from_the_configured_time():
    cand = ps.candidate_for("wake", _at(FRI, 7, 3), CFG)
    assert cand == {"date": "2026-11-20", "class": "weekday", "kind": "wake", "delta_min": 33}
    assert ps.candidate_for("wake", _at(SAT, 10, 0), CFG) is None  # beyond three hours


def test_an_after_midnight_bedtime_belongs_to_the_previous_evening():
    cand = ps.candidate_for("bed", _at(SAT, 0, 40), CFG)
    assert cand == {"date": "2026-11-21", "class": "weekend", "kind": "bed", "delta_min": 130}
    early = ps.candidate_for("bed", _at(FRI, 20, 0), CFG)
    assert early == {"date": "2026-11-21", "class": "weekend", "kind": "bed", "delta_min": -150}


def test_the_candidate_class_is_the_class_of_the_waking_date():
    holiday = {SAT: True, SUN: False}
    sat_bed = ps.candidate_for("bed", _at(FRI, 23, 0), CFG, holiday)
    assert sat_bed["date"] == "2026-11-21"
    assert sat_bed["class"] == "weekday"  # Saturday is a working day


def test_a_configured_bedtime_after_midnight_is_measured_on_the_evening_it_belongs_to():
    config = {"wake_time": "07:00", "sleep_time": "00:30"}
    assert ps.candidate_for("bed", _at(SAT, 1, 10), config)["delta_min"] == 40
    assert ps.candidate_for("bed", _at(FRI, 23, 50), config)["delta_min"] == -40
    assert ps.candidate_for("bed", _at(FRI, 23, 50), config)["date"] == "2026-11-21"


def test_deltas_wrap_to_twelve_hours():
    assert ps.wrap_minutes(130) == 130
    assert ps.wrap_minutes(-130) == -130
    assert ps.wrap_minutes(23 * 60) == -60
    assert ps.wrap_minutes(-23 * 60) == 60
    assert ps.wrap_minutes(720) == -720


def test_a_bedtime_keeps_the_last_deactivation_and_a_wake_the_first_activation():
    wake: list[dict] = []
    assert ps.merge_candidate(
        wake, {"date": "2026-11-20", "kind": "wake", "class": "weekday", "delta_min": 33}, replace=False
    )
    assert not ps.merge_candidate(
        wake, {"date": "2026-11-20", "kind": "wake", "class": "weekday", "delta_min": 50}, replace=False
    )
    assert wake[0]["delta_min"] == 33
    bed: list[dict] = []
    ps.merge_candidate(bed, {"date": "2026-11-21", "kind": "bed", "class": "weekend", "delta_min": 70}, replace=True)
    assert ps.merge_candidate(
        bed, {"date": "2026-11-21", "kind": "bed", "class": "weekend", "delta_min": 130}, replace=True
    )
    assert [c["delta_min"] for c in bed] == [130]


@pytest.mark.parametrize(
    ("domain", "old", "new", "kind", "expected"),
    [
        ("light", "off", "on", "wake", True),
        ("light", "on", "off", "wake", False),
        ("switch", "on", "off", "bed", True),
        ("light", "unavailable", "on", "wake", False),
        ("light", "on", "unknown", "bed", False),
        ("light", None, "on", "wake", False),
        ("light", "on", "on", "wake", False),
        ("event", "2026-11-20T07:00:00.000+00:00", "2026-11-20T23:00:00.000+00:00", "bed", True),
        ("event", "unknown", "2026-11-20T23:00:00.000+00:00", "bed", False),
        ("binary_sensor", "off", "on", "wake", True),
    ],
)
def test_only_real_transitions_between_known_states_count(domain, old, new, kind, expected):
    assert ps.transition_active(domain, old, new, kind) is expected


def test_a_history_read_skips_its_synthetic_start_state():
    rows = [
        (_at(FRI, 0, 0), "on"),  # the window's start state: its old state is unknown
        (_at(FRI, 7, 0), "off"),
        (_at(FRI, 7, 5), "on"),
        (_at(FRI, 7, 6), "unavailable"),
        (_at(FRI, 7, 9), "on"),  # a restart: unavailable -> on is not a real activation
    ]
    assert ps.row_transitions("light", rows, "wake") == [_at(FRI, 7, 5)]


# ── the schedule filter on the recorded TV LED pattern ──────────────────────────────────────────────────


def _tv_led_pattern() -> tuple[list[datetime], list[datetime]]:
    """The 31-day evidence: on at 07:00:01 every day, off at 23:59:00 on 22 nights and by hand 23:15..23:56 on 9."""
    start = date(2026, 10, 17)
    ons = [_at(start + timedelta(days=i), 7, 0, 1) for i in range(31)]
    hand = [(23, 15), (23, 21), (23, 28), (23, 33), (23, 40), (23, 47), (23, 52), (23, 54), (23, 56)]
    offs: list[datetime] = []
    for i in range(31):
        day = start + timedelta(days=i)
        offs.append(_at(day, *hand[i]) if i < len(hand) else _at(day, 23, 59, 0))
    return ons, offs


def test_the_schedule_filter_removes_the_automation_and_keeps_the_hand_offs():
    ons, offs = _tv_led_pattern()
    assert all(ps.schedule_filter(ons, 31))  # 07:00:01 every day
    mask = ps.schedule_filter(offs, 31)
    assert mask == [False] * 9 + [True] * 22
    hand = [o for o, scheduled in zip(offs, mask, strict=True) if not scheduled]
    assert (hand[0].hour, hand[0].minute) == (23, 15)
    assert (hand[-1].hour, hand[-1].minute) == (23, 56)


def test_the_filter_needs_four_days_and_a_share_of_the_window():
    three = [_at(date(2026, 11, 1) + timedelta(days=i), 23, 59) for i in range(3)]
    assert ps.schedule_filter(three, 3) == [False, False, False]  # three days are never a schedule
    four = [_at(date(2026, 11, 1) + timedelta(days=i), 23, 59) for i in range(4)]
    assert all(ps.schedule_filter(four, 4))
    assert ps.schedule_filter(four, 30) == [False] * 4  # 4 of 30 days is under 40 %


def test_the_history_candidates_drop_the_automation_and_keep_one_bed_per_night():
    ons, offs = _tv_led_pattern()
    # Bed signals: the TV LED off events plus a late button press after midnight on the first night.
    button = [_at(date(2026, 10, 18), 0, 30)]  # belongs to the evening of 2026-10-17 (waking date 2026-10-18)
    found, counts = ps.history_candidates(
        {"wake": {"light.tv": ons}, "bed": {"light.tv": offs, "event.btn": button}},
        config=CFG,
        workdays=None,
        days_covered=31,
    )
    assert counts["schedule"] == 31 + 22
    beds = [c for c in found if c["kind"] == "bed"]
    assert len(beds) == 9  # the 22 automation off events are gone
    assert all(c["source"] == "history" for c in found)
    first = next(c for c in beds if c["date"] == "2026-10-18")
    assert first["delta_min"] == 120  # the 00:30 press (later than the 23:15 hand off) wins: the last deactivation
    assert not [c for c in found if c["kind"] == "wake"]  # the TV LED's 07:00:01 is the automation's


# ── weekly update ───────────────────────────────────────────────────────────────────────────────────────


def _cands(cls, kind, deltas, start=date(2026, 11, 1)):
    return [
        {
            "date": (start + timedelta(days=i)).isoformat(),
            "class": cls,
            "kind": kind,
            "delta_min": d,
            "source": "sensor",
        }
        for i, d in enumerate(deltas)
    ]


def test_fewer_than_four_candidates_change_nothing():
    shifts, changes = ps.weekly_schedule_update(
        {}, _cands("weekday", "wake", [30, 30, 30]), max_shift_min=90, step_min=15, today=date(2026, 11, 20)
    )
    assert shifts["weekday"]["wake"] == 0
    assert changes == []


def test_the_weekly_step_the_cap_and_the_rounding():
    cands = _cands("weekday", "wake", [33, 38, 40, 35, 36])  # median 36
    shifts, changes = ps.weekly_schedule_update({}, cands, max_shift_min=90, step_min=15, today=date(2026, 11, 20))
    assert shifts["weekday"]["wake"] == 15  # one step
    assert changes[0]["old_min"] == 0 and changes[0]["new_min"] == 15 and changes[0]["n"] == 5
    again, _ = ps.weekly_schedule_update(shifts, cands, max_shift_min=90, step_min=15, today=date(2026, 11, 27))
    assert again["weekday"]["wake"] == 30
    third, _ = ps.weekly_schedule_update(again, cands, max_shift_min=90, step_min=15, today=date(2026, 12, 4))
    assert third["weekday"]["wake"] == 35  # the median 36 rounded to 5, not another 15
    capped, _ = ps.weekly_schedule_update(
        {}, _cands("weekend", "bed", [150] * 5), max_shift_min=60, step_min=60, today=date(2026, 11, 20)
    )
    assert capped["weekend"]["bed"] == 60


def test_the_median_uses_wrapped_deltas_and_the_newest_twenty():
    old = _cands("weekday", "bed", [0] * 30, start=date(2026, 9, 1))
    new = _cands("weekday", "bed", [80] * 20, start=date(2026, 11, 1))
    shifts, _ = ps.weekly_schedule_update({}, old + new, max_shift_min=90, step_min=60, today=date(2026, 11, 25))
    assert shifts["weekday"]["bed"] == 60  # only the newest twenty (all 80) count
    wrapped = _cands("weekday", "bed", [23 * 60, 23 * 60, 23 * 60, 23 * 60])  # 23 h = -1 h
    got, _ = ps.weekly_schedule_update({}, wrapped, max_shift_min=90, step_min=60, today=date(2026, 11, 25))
    assert got["weekday"]["bed"] == -60


def test_lowering_the_cap_clamps_the_stored_shift_on_read():
    assert _view(weekday=(75, 0), cap=30).shift("weekday", "wake") == 30


def test_seed_rows_flag_thin_pairs_disabled_and_set_the_clamped_median():
    cands = _cands("weekday", "wake", [40, 44, 36, 38]) + _cands("weekend", "bed", [200, 190])
    rows = ps.seed_schedule_rows(cands, {"weekday": {"wake": 10}}, max_shift_min=90)
    wake = rows["weekday|wake"]
    assert wake == {"n": 4, "median_min": 40, "current_min": 10, "new_min": 40, "enabled": True}
    thin = rows["weekend|bed"]
    assert thin["enabled"] is False and thin["new_min"] is None and thin["n"] == 2
    assert (
        ps.seed_schedule_rows(_cands("weekend", "bed", [200] * 4), {}, max_shift_min=90)["weekend|bed"]["new_min"] == 90
    )


def test_trim_keeps_four_hundred_days():
    today = date(2026, 11, 20)
    old = {"date": "2025-01-01", "class": "weekday", "kind": "wake", "delta_min": 1}
    fresh = {"date": "2026-11-19", "class": "weekday", "kind": "wake", "delta_min": 1}
    assert ps.trim_candidates([old, fresh], today) == [fresh]


def test_the_row_key_round_trips():
    assert ps.parse_schedule_row_key(ps.schedule_row_key("weekend", "bed")) == ("weekend", "bed")
    assert ps.parse_schedule_row_key("weekend|sleep") is None
    assert ps.parse_schedule_row_key(None) is None


def test_the_preference_view_buckets_through_the_seam():
    view = pl.PreferenceView(schedule=_view(weekday=(0, 100), weekend=(0, 100), cap=180))
    # 23:40 Friday with a learned 00:10 bedtime: still awake (evening), class of the day the evening belongs to.
    offset, bucket = view.offset_at(_at(FRI, 23, 40), "cold", CFG, in_sleep_window=False)
    assert bucket == "cold|evening|weekday|-"
    # 00:20 Saturday: the night in progress, waking Saturday (weekend).
    asleep = _in_sleep_window(_at(SAT, 0, 20), view.config_at(CFG, _at(SAT, 0, 20)))
    assert asleep is True
    assert view.offset_at(_at(SAT, 0, 20), "cold", CFG, in_sleep_window=asleep)[1] == "cold|night|weekend|-"
    assert offset == 0.0
