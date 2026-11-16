"""Setpoint preference learning, phase 3: rooms, humidity and history backfill (fork Dunky13#1).

Pure parts only: humidity bands, four-part keys and the three-level weekly update, the steering reference, the
learned-room score, and the recorder backfill (schedule filter against the recorded Nest schedule of the target
install, history episodes, seed summary). Spec: docs/preference-learning-spec.md, "Phase 3".
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import preference_backfill as bf
from custom_components.climate_advisor import preference_learning as pl
from custom_components.climate_advisor.automation import _in_sleep_window
from custom_components.climate_advisor.classifier import classify_day
from custom_components.climate_advisor.learning import PreferenceSeedInvalid

TODAY = date(2026, 11, 16)
TZ = ZoneInfo("Europe/Amsterdam")
WAKE, SLEEP = "06:30", "23:00"
BED, KID, LIVING = "sensor.bedroom_temp", "sensor.kid_temp", "sensor.daikin_living_temp"
CONFIG = {"wake_time": WAKE, "sleep_time": SLEEP, "room_temp_entities": [BED, KID, LIVING]}


def _f(c: float) -> float:
    return round(c * 1.8 + 32.0, 2)


# ── humidity band ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rh", "current", "band"),
    [
        (39.9, None, "dry"),
        (40.0, None, "normal"),
        (60.0, None, "normal"),
        (60.1, None, "humid"),
        (41.0, "dry", "dry"),  # leaving dry needs 42
        (42.0, "dry", "normal"),
        (39.0, "normal", "normal"),  # entering dry needs below 38
        (37.9, "normal", "dry"),
        (61.0, "normal", "normal"),  # entering humid needs above 62
        (62.1, "normal", "humid"),
        (59.0, "humid", "humid"),  # leaving humid needs 58
        (57.9, "humid", "normal"),
        (None, "normal", "-"),
        (float("nan"), None, "-"),
        (120.0, None, "-"),
    ],
)
def test_humidity_band_with_hysteresis(rh, current, band):
    assert pl.humidity_band(rh, current) == band


# ── keys ────────────────────────────────────────────────────────────────────────────────────────────────


def test_bucket_key_gains_the_band_and_parsing_accepts_both_lengths():
    at = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)
    assert pl.bucket_key(at, "cold", WAKE, SLEEP, in_sleep_window=False, band="humid") == "cold|evening|weekday|humid"
    assert pl.parse_bucket("cold|evening|weekday") == ("cold", "evening", "weekday", None)
    assert pl.parse_bucket("cold|evening|weekday|dry") == ("cold", "evening", "weekday", "dry")
    assert pl.parse_bucket("cold|evening|weekday|soggy") is None
    assert pl.bucket_parts("cold|evening|weekday|dry") == ("cold", "evening", "weekday")
    assert pl.with_band("cold|evening|weekday") == "cold|evening|weekday|-"
    assert pl.with_band("cold|evening|weekday|dry") == "cold|evening|weekday|dry"


def test_view_looks_up_four_part_then_three_part_then_zero():
    view = pl.PreferenceView({"cold|evening|weekday": 1.0, "cold|evening|weekday|humid": 0.0}, max_offset_f=1.8)
    assert view.offset_for("cold|evening|weekday|dry") == 1.0  # three-part fallback
    assert view.offset_for("cold|evening|weekday|humid") == 0.0  # an explicit four-part 0 wins
    assert view.offset_for("cold|morning|weekday|dry") == 0.0
    humid = pl.PreferenceView({"cold|evening|weekday|humid": 3.0}, max_offset_f=1.8, band="humid")
    at = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)
    assert humid.offset_at(at, "cold", CONFIG, in_sleep_window=False) == (1.8, "cold|evening|weekday|humid")


# ── weekly update levels ────────────────────────────────────────────────────────────────────────────────


def _obs(bucket: str, *deltas: float) -> list[dict]:
    return [{"date": "2026-11-01", "time": "18:00", "bucket": bucket, "delta_f": d} for d in deltas]


def test_weekly_update_uses_the_band_level_with_three_observations():
    obs = _obs("cold|evening|weekday|humid", 2.0, 2.0, 2.0) + _obs("cold|evening|weekday|dry", -1.0)
    offsets, _ = pl.weekly_update({}, obs, max_offset_f=3.6, step_f=3.6, today=TODAY)
    assert offsets["cold|evening|weekday|humid"] == {"offset_f": 2.0, "n": 3, "level": "band", "updated": "2026-11-16"}
    # Too few in the dry band: the band-agnostic key across bands (median of 2, 2, 2, -1).
    assert offsets["cold|evening|weekday|dry"]["level"] == pl.LEVEL_BUCKET
    assert offsets["cold|evening|weekday|dry"]["offset_f"] == 2.0
    assert offsets["cold|evening|weekday"]["level"] == pl.LEVEL_BUCKET
    # The sibling day class has no observations: the day_type|part level across classes and bands.
    assert offsets["cold|evening|weekend"]["level"] == pl.LEVEL_DAYTYPE_PART
    assert "cold|evening|weekend|humid" not in offsets


def test_phase_one_three_part_observations_count_as_band_none_without_a_mirror_row():
    offsets, changes = pl.weekly_update(
        {}, _obs("cold|evening|weekday", 1.0, 1.0, 1.0), max_offset_f=3.6, step_f=3.6, today=TODAY
    )
    # Only band "-" known: the "-" key would mirror the three-part key, so there is one row and one change.
    assert set(offsets) == {"cold|evening|weekday", "cold|evening|weekend"}  # the bucket and its sibling
    assert offsets["cold|evening|weekday"]["offset_f"] == 1.0
    assert [c["bucket"] for c in changes].count("cold|evening|weekday") == 1
    assert not any(c["bucket"].endswith("|-") for c in changes)
    assert pl.PreferenceView(offsets={"cold|evening|weekday": 1.0}).offset_for("cold|evening|weekday|-") == 1.0


def test_a_band_appearing_gives_band_and_dash_rows_and_starts_them_from_the_learned_offset():
    obs = _obs("cold|evening|weekday|-", 1.0, 1.0, 1.0) + _obs("cold|evening|weekday|dry", 3.6, 3.6, 3.6)
    offsets, _ = pl.weekly_update(
        {"cold|evening|weekday": {"offset_f": 2.0}}, obs, max_offset_f=3.6, step_f=0.9, today=TODAY
    )
    assert offsets["cold|evening|weekday|dry"]["offset_f"] == 2.9  # 2.0 + one 0.9 step towards 3.6, not 0 + 0.9
    assert offsets["cold|evening|weekday|-"]["offset_f"] == 1.1  # 2.0 - one step towards 1.0
    assert offsets["cold|evening|weekday|dry"]["level"] == pl.LEVEL_BAND


def test_upgrading_from_phase_one_keeps_a_learned_offset():
    # A phase 1 install: +3.6 F learned under the three-part key. The first phase 3 week must not reset it to 0.9.
    phase1 = {"cold|night|weekday": {"offset_f": 3.6, "n": 20, "level": "bucket", "updated": "2026-11-09"}}
    obs = _obs("cold|night|weekday|-", 3.6, 3.6, 3.6) + _obs("cold|night|weekday|normal", 3.6, 3.6, 3.6)
    offsets, changes = pl.weekly_update(phase1, obs, max_offset_f=3.6, step_f=0.9, today=TODAY)
    view = pl.PreferenceView(offsets={k: v["offset_f"] for k, v in offsets.items()}, band="normal")
    assert view.offset_for("cold|night|weekday|normal") == 3.6
    assert view.offset_for("cold|night|weekday|-") == 3.6
    assert {c["bucket"] for c in changes} == {"cold|night|weekend"}  # only the sibling moves (from 0)


def test_seed_offsets_ignore_the_weekly_step_but_keep_the_cap():
    summary = pl.seed_offsets(_obs("cold|morning|weekday|-", 5.0, 6.0, 7.0), max_offset_f=3.6)
    # Band "-" only: one row, the three-part key (no "-" mirror).
    assert summary["cold|morning|weekday"] == {"n": 3, "median_f": 6.0, "offset_f": 3.6, "level": "bucket"}
    assert "cold|morning|weekday|-" not in summary
    small = pl.seed_offsets(_obs("cold|morning|weekday|-", 2.4, 2.5, 2.6), max_offset_f=3.6)
    assert small["cold|morning|weekday"]["offset_f"] == 2.5  # not limited to the 0.9 weekly step


def test_drop_parts_restarts_only_the_changed_part():
    offsets = {"cold|morning|weekday": {}, "cold|morning|weekday|dry": {}, "cold|evening|weekday": {}}
    assert set(pl.drop_parts(offsets, {"morning"})) == {"cold|evening|weekday"}


def test_observations_in_frame_match_the_part_reference():
    obs = [
        {"bucket": "cold|morning|weekday|-", "delta_f": 1.0, "ref": BED},
        {"bucket": "cold|morning|weekday|-", "delta_f": 1.0, "ref": None},
        {"bucket": "cold|evening|weekday", "delta_f": 1.0},  # phase 1: no ref = the thermostat
    ]
    kept = pl.observations_in_frame(obs, {"morning": BED, "evening": None})
    assert kept == [obs[0], obs[2]]


# ── steering reference ──────────────────────────────────────────────────────────────────────────────────

MORNING = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
NIGHT = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


def test_feature_off_steers_by_the_sleep_sensor_only():
    cfg = {**CONFIG, "sleep_indoor_temp_entity": BED, "room_pin_morning": KID}
    assert pl.steering_reference(NIGHT, cfg, in_sleep_window=True, learned_rooms=None).entity == BED
    ref = pl.steering_reference(MORNING, cfg, in_sleep_window=False, learned_rooms=None)
    assert ref == pl.SteeringReference(None, None, "thermostat", "morning")


def test_sleep_sensor_beats_a_pin_and_a_learned_room_at_night():
    cfg = {**CONFIG, "sleep_indoor_temp_entity": BED, "room_pin_night": KID}
    ref = pl.steering_reference(NIGHT, cfg, in_sleep_window=True, learned_rooms={"night": LIVING})
    assert ref == pl.SteeringReference(BED, "sleep", "sleep_sensor", "night")


def test_pin_beats_the_learned_room():
    learned = {"morning": BED}
    pinned = pl.steering_reference(
        MORNING, {**CONFIG, "room_pin_morning": KID}, in_sleep_window=False, learned_rooms=learned
    )
    assert pinned == pl.SteeringReference(KID, "room", "pinned", "morning")
    thermostat = {**CONFIG, "room_pin_morning": "thermostat"}
    assert pl.steering_reference(MORNING, thermostat, in_sleep_window=False, learned_rooms=learned) == (
        pl.SteeringReference(None, None, "pinned", "morning")
    )


def test_auto_uses_the_learned_room_else_the_thermostat():
    assert pl.steering_reference(MORNING, CONFIG, in_sleep_window=False, learned_rooms={"morning": BED}) == (
        pl.SteeringReference(BED, "room", "learned", "morning")
    )
    assert pl.steering_reference(MORNING, CONFIG, in_sleep_window=False, learned_rooms={}).source == "thermostat"
    # A learned room no longer selected is ignored.
    gone = {**CONFIG, "room_temp_entities": [KID]}
    assert pl.steering_reference(MORNING, gone, in_sleep_window=False, learned_rooms={"morning": BED}).entity is None


def test_a_stale_pin_reads_as_auto():
    cfg = {**CONFIG, "room_pin_morning": "sensor.removed"}
    assert pl.room_pin(cfg, "morning") == "auto"
    assert (
        pl.steering_reference(MORNING, cfg, in_sleep_window=False, learned_rooms={"morning": BED}).source == "learned"
    )


def test_references_by_part():
    refs = pl.references_by_part({**CONFIG, "room_pin_evening": KID}, {"morning": BED})
    assert refs == {"night": None, "morning": BED, "afternoon": None, "evening": KID}


# ── learned room ────────────────────────────────────────────────────────────────────────────────────────


def _spread(center: float, width: float, n: int) -> list[float]:
    return [round(center + width * ((i % 5) - 2) / 2, 2) for i in range(n)]


def test_score_chooses_a_consistent_room_below_the_gate():
    raises = {"thermostat": _spread(66, 1.0, 12), BED: _spread(64, 0.2, 12)}
    background = {"thermostat": _spread(66, 1.2, 40), BED: _spread(64, 1.2, 40)}
    chosen, ratios, counts = pl.score_rooms(raises, background)
    assert chosen == BED
    assert ratios[BED] < 0.7 and ratios[BED] <= ratios["thermostat"] - 0.15
    assert counts == {"thermostat": 12, BED: 12}


def test_score_needs_the_margin_over_the_thermostat():
    raises = {"thermostat": _spread(66, 0.25, 12), BED: _spread(64, 0.2, 12)}
    background = {"thermostat": _spread(66, 1.2, 40), BED: _spread(64, 1.2, 40)}
    assert pl.score_rooms(raises, background)[0] is None


def test_score_needs_ten_samples_and_a_thermostat_ratio():
    background = {"thermostat": _spread(66, 1.2, 40), BED: _spread(64, 1.2, 40)}
    few = {"thermostat": _spread(66, 1.0, 12), BED: _spread(64, 0.2, 9)}
    chosen, ratios, counts = pl.score_rooms(few, background)
    assert chosen is None and BED not in ratios and counts[BED] == 9
    assert pl.score_rooms({BED: _spread(64, 0.2, 12)}, background)[0] is None


def test_mad_floor_keeps_a_flat_room_from_dividing_by_zero():
    raises = {"thermostat": _spread(66, 1.0, 12), BED: [64.0] * 12}
    background = {"thermostat": _spread(66, 1.2, 40), BED: [64.0] * 40}
    _chosen, ratios, _ = pl.score_rooms(raises, background)
    assert ratios[BED] == 1.0  # 0.1 / 0.1


def test_whole_degree_sensor_is_scored_but_never_chosen():
    raises = {"thermostat": _spread(66, 1.0, 12), LIVING: _spread(64, 0.2, 12)}
    background = {"thermostat": _spread(66, 1.2, 40), LIVING: _spread(64, 1.2, 40)}
    chosen, ratios, _ = pl.score_rooms(raises, background, whole=frozenset({LIVING}))
    assert chosen is None and LIVING in ratios


def test_whole_series_is_judged_in_the_unit_the_readings_were_taken_in():
    # A whole-C sensor on a C install (stored as 66.2, 68.0, 69.8 F) is whole; the same readings on a F install are not.
    whole_c = [_f(c) for c in (19, 20, 21, 20, 19)]
    assert pl.is_whole_series(whole_c, "celsius")
    assert not pl.is_whole_series(whole_c, "fahrenheit")
    assert not pl.is_whole_series([_f(c) for c in (19, 20.5, 21, 20, 19)], "celsius")
    assert pl.is_whole_series([66.0, 67.0, 68.0, 67.0, 66.0], "fahrenheit")  # a whole-F sensor
    assert not pl.is_whole_series([66.0, 67.0], "fahrenheit")  # too few to tell
    assert not pl.is_whole_series([66.2, 66.4, 66.9, 67.3, 66.0], "fahrenheit")  # a 0.1-degree sensor


def test_a_tenth_degree_celsius_sensor_is_not_mistaken_for_a_whole_fahrenheit_one():
    # 21.1 C is 69.98 F: within 0.02 of a whole F, but a 0.1-C sensor on a C install, never whole.
    readings = [round(_f(c), 2) for c in (21.1, 21.1, 20.9, 21.1, 21.1)]
    assert readings[0] == 69.98
    assert not pl.is_whole_series(readings, "celsius")


def test_raise_samples_take_the_newest_sixty_raises_of_the_part():
    obs = [{"bucket": "cold|morning|weekday|-", "delta_f": 1.0, "rooms": {BED: float(i)}} for i in range(70)]
    obs.append({"bucket": "cold|evening|weekday|-", "delta_f": 1.0, "rooms": {BED: 99.0}})
    obs.append({"bucket": "cold|morning|weekday|-", "delta_f": -1.0})  # a lower carries no rooms
    samples = pl.raise_samples(obs, "morning")
    assert samples[BED] == [float(i) for i in range(10, 70)]


def test_closed_episode_keeps_rooms_only_for_a_raise():
    ctx = {"ref": None, "band": "dry", "rh": 35.0, "rooms": {BED: 64.0}}
    common = {"bucket": "cold|morning|weekday|dry", "base_f": 68.0, "target_f": 68.0, "context": ctx}
    raise_ep, _ = pl.extend_episode(None, at=MORNING, old_f=68.0, new_f=70.0, **common)
    lower_ep, _ = pl.extend_episode(None, at=MORNING, old_f=68.0, new_f=66.0, **common)
    assert pl.close_episode(raise_ep)["rooms"] == {BED: 64.0}
    lowered = pl.close_episode(lower_ep)
    assert "rooms" not in lowered and lowered["band"] == "dry" and lowered["ref"] is None and lowered["rh"] == 35.0


# ── backfill: the recorded Nest schedule ────────────────────────────────────────────────────────────────

# Local clock, setpoint (C): the thermostat's own schedule on the target install, every day within +-2 minutes.
NEST_SCHEDULE = [((1, 30), 15.9), ((6, 15), 16.0), ((7, 15), 16.8), ((8, 45), 15.9), ((21, 30), 16.1), ((23, 59), 18.0)]
FIRST_DAY = date(2026, 10, 18)  # two weeks, across the 2026-10-25 DST change
DAYS = 14

# day index -> occupant changes (local h, m, s, setpoint C); bursts within a minute, different every day.
OCCUPANT = {
    0: [
        (6, 40, 0, 16.5),
        (6, 40, 10, 17.0),
        (6, 40, 20, 17.5),
        (6, 40, 30, 18.0),
        (6, 40, 40, 18.5),
        (6, 40, 50, 19.0),
    ],
    1: [(7, 30, 0, 20.0)],
    3: [(13, 51, 0, 17.0), (13, 51, 15, 19.5), (13, 51, 30, 20.0), (13, 51, 45, 20.5), (14, 8, 0, 21.0)],
    5: [(9, 10, 0, 19.5), (18, 0, 0, 20.0), (20, 15, 0, 17.0)],
    7: [(6, 55, 0, 20.5)],  # 2026-10-25, the DST day
    9: [(12, 20, 0, 20.0), (12, 25, 0, 20.0 + 0.2)],
    11: [(10, 5, 0, 19.0), (10, 9, 0, 16.0)],  # an undo inside one episode
    12: [(17, 40, 0, 20.0), (20, 50, 0, 16.5)],
}


def _local(day: int, h: int, m: int, s: int = 0) -> datetime:
    return datetime.combine(FIRST_DAY + timedelta(days=day), datetime.min.time(), TZ).replace(
        hour=h, minute=m, second=s
    )


# The late 18.0 C slot in the real data also fired at these times (mostly 23:59): day index -> local (h, m).
LATE_OUTLIERS = {0: (23, 15), 1: (23, 18), 2: (23, 22), 3: (23, 45), 4: (23, 46), 5: (23, 54), 6: (23, 56)}


def _nest_history(late: dict[int, tuple[int, int]] | None = None):
    """``(rows, truth)``: climate rows in UTC with setpoints in F, and per change whether it is the schedule.

    ``late`` moves the last (18.0 C) slot of a day to that exact local time.
    """
    events: list[tuple[datetime, float, bool]] = []
    for day in range(DAYS):
        for i, ((h, m), c) in enumerate(NEST_SCHEDULE):
            jitter = ((day + i) % 5) - 2  # -2..+2 minutes, never the same on consecutive days
            if late and day in late and i == len(NEST_SCHEDULE) - 1:
                (h, m), jitter = late[day], 0
            events.append((_local(day, h, m) + timedelta(minutes=jitter), c, True))
        for h, m, s, c in OCCUPANT.get(day, []):
            events.append((_local(day, h, m, s), c, False))
    events.sort()
    rows = [bf.ClimateRow(at=_local(0, 0, 0).astimezone(UTC), mode="heat", setpoint_f=_f(18.0), current_f=_f(18.0))]
    truth: list[bool] = []
    setpoint = 18.0
    for at, c, sched in events:
        if abs(c - setpoint) < 1e-9:
            continue  # no change recorded
        rows.append(bf.ClimateRow(at=at.astimezone(UTC), mode="heat", setpoint_f=_f(c), current_f=_f(18.5)))
        truth.append(sched)
        setpoint = c
    return rows, truth


def test_schedule_filter_removes_every_nest_slot_and_keeps_every_burst():
    rows, truth = _nest_history()
    changes = bf.setpoint_changes(rows)
    assert len(changes) == len(truth)
    mask = bf.schedule_mask(changes, TZ, DAYS)
    assert mask == truth
    kept = [c.at.astimezone(TZ) for c, sched in zip(changes, mask, strict=True) if not sched]
    assert (kept[0].hour, kept[0].minute) == (6, 40)  # the first burst survives intact
    assert sum(1 for t in kept if (t.hour, t.minute) == (6, 40)) == 6
    assert any((t.hour, t.minute) == (14, 8) for t in kept)


def test_a_slot_on_too_few_days_is_not_the_schedule():
    rows, _ = _nest_history()
    changes = bf.setpoint_changes(rows)
    # Five days of a 14-day window is below 40 %.
    assert not any(bf.schedule_mask([c for c in changes if c.at.astimezone(TZ).day <= 22], TZ, 14))


def test_a_person_in_home_assistant_is_never_the_schedule():
    rows, truth = _nest_history()
    changes = bf.setpoint_changes(rows)
    first_sched = truth.index(True)
    changes[first_sched] = bf.SetpointChange(changes[first_sched].at, 60.0, changes[first_sched].new_f, user_id="abc")
    mask = bf.schedule_mask(changes, TZ, DAYS)
    assert mask[first_sched] is False
    assert sum(mask) == sum(truth) - 1


def test_changes_outside_heat_or_in_eco_are_ignored():
    t0 = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    rows = [
        bf.ClimateRow(t0, "heat", 60.0),
        bf.ClimateRow(t0 + timedelta(minutes=1), "heat", 62.0, preset="eco"),
        bf.ClimateRow(t0 + timedelta(minutes=2), "heat", 64.0),  # leaving eco: the old state was eco
        bf.ClimateRow(t0 + timedelta(minutes=3), "cool", 70.0),
        bf.ClimateRow(t0 + timedelta(minutes=4), "heat", 66.0),
        bf.ClimateRow(t0 + timedelta(minutes=5), "heat", None),
    ]
    assert [(c.old_f, c.new_f) for c in bf.setpoint_changes(rows)] == [(70.0, 66.0)]


def _seed(**overrides):
    rows = overrides.pop("rows", None) or _nest_history()[0]
    start = _local(0, 0, 0).astimezone(UTC)
    end = _local(DAYS, 0, 0).astimezone(UTC)
    hours = [start + timedelta(hours=h) for h in range(DAYS * 24 + 1)]
    quarters = [start + timedelta(minutes=15 * q) for q in range(DAYS * 96 + 1)]
    args = {
        "rooms": {BED: [(t, _f(17.0 + (t.minute // 15) * 0.3)) for t in quarters]},
        "humidity": [(start, 55.0), (_local(5, 0, 0).astimezone(UTC), 65.0)],
        "outdoor": [(t, 45.0 + (t.hour % 12)) for t in hours],
        "start": start,
        "end": end,
        "tz": TZ,
        "config": CONFIG,
        "classify": lambda forecast: classify_day(forecast),
        "base_floor": lambda _at, _c, asleep: 62.0 if asleep else 68.0,
        "max_offset_f": 3.6,
    }
    args.update(overrides)
    args.setdefault("asleep", lambda loc, cfg=args["config"]: _in_sleep_window(loc, cfg))
    return bf.build_seed(rows, **args)


def test_seed_from_the_nest_history():
    seed = _seed()
    obs = seed["observations"]
    assert seed["window"]["days"] == DAYS
    assert seed["counts"]["schedule"] == DAYS * len(NEST_SCHEDULE)  # every slot changes the setpoint every day
    # One per occupant episode (12 episodes): day 3 is two (17 minutes apart); the day 11 undo is discarded.
    assert seed["counts"]["observations"] == len(obs) == 11
    assert all(o["source"] == "history" and o["ref"] is None and o["bucket"].count("|") == 3 for o in obs)
    burst = obs[0]
    assert (burst["date"], burst["time"]) == ("2026-10-18", "06:40")
    assert burst["delta_f"] == round(_f(19.0) - 68.0, 2)
    assert burst["bucket"].split("|")[1:] == ["morning", "weekend", "normal"]  # Sunday, 55 % RH
    assert burst["rooms"][BED] and burst["rooms"]["thermostat"] == _f(18.5)
    lower = next(o for o in obs if o["date"] == "2026-10-23" and o["time"] == "20:15")
    assert "rooms" not in lower and lower["bucket"].endswith("|humid") and lower["rh"] == 65.0
    assert seed["summary"] and all(abs(v["offset_f"]) <= 3.6 for v in seed["summary"].values())
    background = seed["background"]
    assert set(background) == {"night", "morning", "afternoon", "evening"}
    assert all(len(v) <= pl.BACKGROUND_RESERVOIR for part in background.values() for v in part.values())
    assert "thermostat" in background["morning"] and BED in background["morning"]


def _closed_at(o) -> datetime:
    return datetime.combine(date.fromisoformat(o["date"]), time.fromisoformat(o["time"]), TZ)


def test_history_stops_where_live_recording_took_over():
    full = _seed()["observations"]
    assert all(pl.is_aware_iso(o["start"]) for o in full)
    # Live recording took over at 12:35 on day 9: the 12:20-12:25 episode ends within the 15-minute episode gap of it
    # (it would have been that live episode), so it and everything after it are live recording's own.
    seed = _seed(live_since=_local(9, 12, 35))
    before = [o for o in full if _closed_at(o) < _local(9, 12, 20)]
    assert seed["observations"] == before
    assert seed["counts"]["live"] == len(full) - len(before) > 0


def test_live_since_is_the_earliest_live_episode_and_ignores_history():
    obs = [
        {"date": "2026-11-03", "time": "07:10", "start": "2026-11-03T07:00:00+01:00", "bucket": B4, "delta_f": 1.0},
        {"date": "2026-11-02", "time": "07:10", "bucket": B4, "delta_f": 1.0},  # before 0.9.41.2: no start
        {"date": "2026-10-01", "time": "07:10", "start": "2026-10-01T07:00:00+02:00", "source": "history"},
    ]
    assert pl.live_since(obs, None, TZ) == datetime(2026, 11, 2, 7, 10, tzinfo=TZ)
    assert pl.live_since(obs, {"start": "2026-11-01T06:00:00+01:00"}, TZ) == datetime(2026, 11, 1, 6, 0, tzinfo=TZ)
    assert pl.live_since(obs[2:], None, TZ) is None


def test_with_a_sleep_sensor_history_changes_in_the_sleep_window_are_left_out():
    # That sensor's remote offset moved the setpoint the thermostat reported; the recorder does not keep the offset,
    # so those changes have no known reference frame (spec: reference frames).
    rows, _ = _nest_history(LATE_OUTLIERS)
    plain = _seed(rows=rows)
    sensed = _seed(rows=rows, config={**CONFIG, "sleep_indoor_temp_entity": BED})
    night = [o for o in plain["observations"] if o["time"].startswith("23:")]
    assert night and sensed["counts"]["sleep_sensor"] > 0
    assert not [o for o in sensed["observations"] if o["time"].startswith("23:")]
    assert len(sensed["observations"]) == len(plain["observations"]) - len(night)


def test_seed_skips_days_without_outdoor_data_and_stale_rooms():
    seed = _seed(outdoor=[], rooms={BED: [(_local(0, 0, 0).astimezone(UTC), 64.0)]})
    assert seed["observations"] == [] and seed["counts"]["no_outdoor"] > 0
    seed = _seed(rooms={BED: [(_local(0, 0, 0).astimezone(UTC), 64.0)]})
    assert BED not in seed["observations"][0]["rooms"]  # older than 30 minutes at the burst


def test_late_slot_outliers_survive_the_filter_and_land_in_the_night_bucket_at_the_sleep_floor():
    # Kept at +-10 minutes as specified. Of the late slot's outliers, those with fewer than 6 of 14 days within 10
    # minutes of them survive the filter (23:15, 23:18, 23:22, 23:45, 23:46); 23:54 and 23:56 sit within 10 minutes
    # of the usual 23:59 and are removed with it.
    rows, _ = _nest_history(LATE_OUTLIERS)
    changes = bf.setpoint_changes(rows)
    mask = bf.schedule_mask(changes, TZ, DAYS)
    late = {
        (t.hour, t.minute): sched
        for c, sched in zip(changes, mask, strict=True)
        if abs(c.new_f - _f(18.0)) < 0.01 and (t := c.at.astimezone(TZ)).hour == 23 and c.old_f < c.new_f
    }
    survivors = [(23, 15), (23, 18), (23, 22), (23, 45), (23, 46)]
    assert sorted(k for k, sched in late.items() if not sched) == survivors
    assert late[(23, 54)] and late[(23, 56)]
    # After the 23:00 bedtime the 60-minute bed rule does not apply: each survivor is a night episode whose delta
    # against an 18 C sleep floor is 0, so it confirms that floor instead of moving the night offset.
    seed = _seed(rows=rows, base_floor=lambda _at, _c, asleep: _f(18.0) if asleep else 68.0)
    at_late = {o["time"]: o for o in seed["observations"] if o["time"].startswith("23:")}
    assert sorted(at_late) == [f"{h}:{m:02d}" for h, m in survivors]
    assert all(o["bucket"].split("|")[1] == "night" and o["delta_f"] == 0.0 for o in at_late.values())
    # With bedtime at midnight the same changes fall in the hour before it and are never learned from.
    late_bed = _seed(rows=rows, config={**CONFIG, "sleep_time": "00:00"})
    assert not any(o["time"].startswith("23:") for o in late_bed["observations"])


def test_episode_exclusion_before_wake_applies_to_history():
    seed = _seed(config={**CONFIG, "wake_time": "07:00"})  # 06:40 is now within 60 minutes before wake
    assert all(o["time"] != "06:40" for o in seed["observations"])


# ── learning engine state (phase 3) ─────────────────────────────────────────────────────────────────────


def _engine(tmp_path):
    from custom_components.climate_advisor.learning import LearningEngine

    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


B4 = "cold|morning|weekday|-"
EVENING = "cold|evening|weekday|-"
SEED_WINDOW = {"start": "2026-10-17T00:00:00+00:00", "end": "2026-11-16T00:00:00+00:00", "days": 30}


def test_phase_one_state_loads_with_band_none_and_bad_phase_three_fields_are_stripped(tmp_path):
    engine = _engine(tmp_path)
    engine._state.preference_observations = [
        {"date": "2026-11-01", "time": "08:00", "bucket": "cold|morning|weekday", "delta_f": 1.0},
        {"date": "2026-11-02", "time": "08:00", "bucket": B4, "delta_f": 1.0, "rooms": {BED: "warm"}, "band": "x"},
    ]
    engine._state.preference_offsets = {"cold|morning|weekday": {"offset_f": 1.0}}
    engine._state.room_background = {"morning": {BED: [64.0, "x", 65.0]}, "noon": {}}
    engine._state.learned_rooms = {"morning": {"entity": BED, "ratios": {}, "ref": None}, "evening": "junk"}
    engine._state.preference_seed = {"observations": "junk", "summary": {}}
    engine.save_state()

    reloaded = _engine(tmp_path)
    obs = reloaded._state.preference_observations
    assert [o["bucket"] for o in obs] == [B4, B4]
    assert "rooms" not in obs[1] and "band" not in obs[1]
    assert reloaded.preference_offsets() == {"cold|morning|weekday": 1.0}  # the band-agnostic level is kept as is
    assert reloaded._state.room_background == {"morning": {BED: [64.0, 65.0]}}
    assert reloaded.learned_room_map() == {"morning": BED}
    assert reloaded._state.preference_seed is None


def test_load_drops_invalid_learned_rooms_seed_rows_and_readings_with_a_warning(tmp_path, caplog):
    engine = _engine(tmp_path)
    good_room = {"entity": BED, "ref": None, "ratios": {BED: 0.4}, "counts": {BED: 12}, "whole": [], "n": 12}
    engine._state.learned_rooms = {
        "morning": {**good_room, "updated": "2026-11-09"},
        "evening": {**good_room, "ratios": {BED: "low"}},
        "night": {**good_room, "counts": {BED: -1}},
        "afternoon": {**good_room, "updated": "yesterday"},
    }
    engine._state.room_background = {"morning": {BED: [64.0, float("nan"), 65.0], KID: "64"}}
    engine._state.preference_seed = {
        "window": SEED_WINDOW,
        "counts": {"observations": 1},
        "observations": [{"date": "2026-10-20", "time": "07:00", "bucket": B4, "delta_f": 1.0, "context_only": "y"}],
        "summary": {
            B4: {"n": 3, "median_f": 1.0, "offset_f": 1.0, "level": "band"},
            EVENING: {"n": 3, "median_f": 1.0, "offset_f": "big", "level": "band"},
            "cold|night|weekday|-": {"n": 3, "median_f": float("inf"), "offset_f": 1.0},
        },
        "background": {"morning": {BED: [63.0, "x"]}},
    }
    engine.save_state()
    caplog.set_level("WARNING")
    reloaded = _engine(tmp_path)
    assert set(reloaded._state.learned_rooms) == {"morning"}
    assert reloaded._state.room_background == {"morning": {BED: [64.0, 65.0]}}
    seed = reloaded._state.preference_seed
    assert set(seed["summary"]) == {B4} and seed["observations"] == []
    assert seed["background"] == {"morning": {BED: [63.0]}}
    for what in ("learned room", "history seed row", "room background readings", "room background entity"):
        assert f"Preference state entry dropped: what={what} reason=" in caplog.text, what


@pytest.mark.parametrize(
    "seed",
    [
        {"observations": [], "summary": {}, "counts": {}},  # no window
        {"observations": [], "summary": {}, "counts": {"n": 1.5}, "window": SEED_WINDOW},
        {"observations": [], "summary": {}, "counts": {}, "window": {**SEED_WINDOW, "start": "now"}},
    ],
)
def test_a_seed_with_a_malformed_frame_is_dropped(tmp_path, seed, caplog):
    engine = _engine(tmp_path)
    engine._state.preference_seed = seed
    engine.save_state()
    assert _engine(tmp_path)._state.preference_seed is None
    assert "Preference state entry dropped: what=history seed reason=malformed" in caplog.text


def test_storing_a_seed_drops_invalid_rows_with_a_warning_and_logs_what_was_kept(tmp_path, caplog):
    engine = _engine(tmp_path)
    caplog.set_level("INFO")
    good = {"n": 3, "median_f": 1.0, "offset_f": 1.0}
    summary = {B4: good, "cold|morning|weekday|dry": {"n": 3, "median_f": 1.0}}
    engine.store_preference_seed({"observations": [], "summary": summary, "window": SEED_WINDOW, "counts": {}})
    assert engine._state.preference_seed["summary"] == {B4: good}
    assert "History seed entry dropped: what=history seed row" in caplog.text
    assert "Preference history seed stored: observations=0 buckets=1" in caplog.text


def test_load_strips_a_malformed_episode_start_and_keeps_the_observation(tmp_path, caplog):
    engine = _engine(tmp_path)
    good = {"date": "2026-11-02", "time": "07:10", "bucket": B4, "delta_f": 1.0}
    engine._state.preference_observations = [{**good, "start": "07:00"}, {**good, "start": "2026-11-02T07:00:00+01:00"}]
    engine.save_state()
    caplog.set_level("WARNING")
    naive, aware = _engine(tmp_path)._state.preference_observations
    assert "start" not in naive and aware["start"] == "2026-11-02T07:00:00+01:00"
    assert "Preference state entry dropped: what=observation field start reason=07:00" in caplog.text


def test_an_invalid_seed_is_refused_and_the_earlier_seed_kept(tmp_path, caplog):
    engine = _engine(tmp_path)
    engine.store_preference_seed({"observations": [], "summary": {}, "window": SEED_WINDOW, "counts": {}})
    earlier = engine._state.preference_seed
    with pytest.raises(PreferenceSeedInvalid):
        engine.store_preference_seed({"observations": [], "summary": {}, "counts": {}})
    assert engine._state.preference_seed is earlier
    assert "Preference history seed rejected: reason=invalid_frame previous_seed_kept=True" in caplog.text


def test_reset_preferences_clears_the_phase_three_state(tmp_path):
    engine = _engine(tmp_path)
    engine.sample_room_background("morning", {BED: 64.0})
    engine._state.learned_rooms = {"morning": {"entity": BED, "ratios": {}}}
    engine.store_preference_seed({"observations": [], "summary": {}, "window": SEED_WINDOW, "counts": {}})
    assert engine._state.preference_seed is not None
    engine.reset("preferences")
    assert (engine._state.room_background, engine._state.learned_rooms, engine._state.preference_seed) == ({}, {}, None)


def test_background_reservoir_keeps_the_newest_200(tmp_path):
    engine = _engine(tmp_path)
    for i in range(250):
        engine.sample_room_background("night", {BED: float(i)})
    assert engine._state.room_background["night"][BED] == [float(i) for i in range(50, 250)]


def test_first_reconcile_stamps_legacy_observations_then_a_change_restarts_the_part(tmp_path):
    engine = _engine(tmp_path)
    engine._state.preference_observations = [{"date": "2026-11-01", "bucket": B4, "delta_f": 1.0}]
    engine._state.preference_offsets = {
        B4: {"offset_f": 1.0},
        "cold|morning|weekday": {"offset_f": 1.0},
        "cold|evening|weekday|-": {"offset_f": 0.5},
    }
    refs = {"night": BED, "morning": None, "afternoon": None, "evening": None}
    assert engine.reconcile_references(refs, TODAY, legacy=refs) == []
    assert engine._state.preference_observations[0]["ref"] is None
    assert engine.reconcile_references(refs, TODAY, legacy=refs) == []
    assert engine.reconcile_references({**refs, "morning": KID}, TODAY, legacy=refs) == ["morning"]
    assert engine.preference_offsets() == {"cold|evening|weekday|-": 0.5}
    assert engine._state.preference_history[-1] == {
        "date": "2026-11-16",
        "kind": "reference",
        "part": "morning",
        "old_ref": None,
        "new_ref": KID,
    }
    assert len(engine._state.preference_observations) == 1  # kept: counts again if the reference returns


def test_first_reconcile_with_a_pin_already_set_restarts_that_part(tmp_path, caplog):
    # Upgrading with a morning pin chosen in the same options save: the phase 1 morning offsets were learned at the
    # thermostat (the legacy frame), so they restart; the night keeps the sleep sensor's frame and its offset.
    engine = _engine(tmp_path)
    engine._state.preference_observations = [
        {"date": "2026-11-01", "bucket": B4, "delta_f": 1.0},
        {"date": "2026-11-01", "bucket": "cold|night|weekday|-", "delta_f": 1.0},
    ]
    engine._state.preference_offsets = {
        "cold|morning|weekday": {"offset_f": 1.0},
        "cold|night|weekday": {"offset_f": 2.0},
    }
    legacy = {"night": BED, "morning": None, "afternoon": None, "evening": None}
    caplog.set_level("INFO")
    assert engine.reconcile_references({**legacy, "morning": KID}, TODAY, legacy=legacy) == ["morning"]
    assert engine.preference_offsets() == {"cold|night|weekday": 2.0}
    assert [o["ref"] for o in engine._state.preference_observations] == [None, BED]
    assert "Preference reference changed, offsets restart from 0: part=morning" in caplog.text


def test_reconcile_prunes_the_background_of_rooms_no_longer_selected(tmp_path):
    engine = _engine(tmp_path)
    engine.sample_room_background("morning", {BED: 64.0, KID: 65.0, "thermostat": 66.0})
    refs = dict.fromkeys(pl.PARTS_OF_DAY)
    engine.reconcile_references(refs, TODAY, legacy=refs, rooms=(BED,))
    assert engine._state.room_background == {"morning": {BED: [64.0], "thermostat": [66.0]}}


def _raise_obs(n: int, *, bed: float, spread_bed: float, therm_spread: float) -> list[dict]:
    return [
        {
            "date": f"2026-11-{1 + i % 9:02d}",
            "time": "07:00",
            "bucket": B4,
            "delta_f": 2.0,
            "ref": None,
            "rooms": {BED: bed + spread_bed * ((i % 5) - 2) / 2, "thermostat": 66 + therm_spread * ((i % 5) - 2) / 2},
        }
        for i in range(n)
    ]


def test_weekly_update_learns_a_room_and_restarts_that_part_in_the_room_frame(tmp_path, caplog):
    engine = _engine(tmp_path)
    engine._state.preference_observations = _raise_obs(12, bed=64.13, spread_bed=0.2, therm_spread=1.0)
    for i in range(40):
        engine.sample_room_background("morning", {BED: 64 + 1.2 * ((i % 5) - 2) / 2, "thermostat": 66 + ((i % 5) - 2)})
    thermostat = dict.fromkeys(pl.PARTS_OF_DAY)
    engine.reconcile_references(thermostat, TODAY, legacy=thermostat)
    caplog.set_level("INFO")
    rooms = (BED,)

    def refs(learned):
        return pl.references_by_part({**CONFIG, "room_temp_entities": list(rooms)}, learned)

    changes = engine.run_preference_update(
        TODAY, max_offset_f=3.6, step_f=0.9, unit="fahrenheit", rooms=rooms, references=refs
    )
    assert engine.learned_room_map()["morning"] == BED
    assert engine._state.learned_rooms["morning"]["ref"] == BED
    assert "Learned room changed: part=morning" in caplog.text
    # The thermostat-frame observations no longer count for the morning: nothing learned there this week.
    assert changes == [] and engine.preference_offsets() == {}


def test_whole_degree_room_is_not_learned(tmp_path):
    engine = _engine(tmp_path)
    engine._state.preference_observations = _raise_obs(12, bed=_f(18), spread_bed=0.0, therm_spread=1.0)
    for i in range(40):
        engine.sample_room_background("morning", {BED: _f(17 + i % 3), "thermostat": 66 + ((i % 5) - 2)})
    engine.run_preference_update(TODAY, max_offset_f=3.6, step_f=0.9, unit="celsius", rooms=(BED,))
    assert engine.learned_room_map()["morning"] is None
    assert BED in engine._state.learned_rooms["morning"]["whole"]


def _seed_state(engine):
    engine.store_preference_seed(
        {
            "window": SEED_WINDOW,
            "counts": {"changes": 9, "schedule": 5, "no_outdoor": 0, "observations": 2},
            "observations": [
                {"date": "2026-10-20", "time": "07:00", "bucket": B4, "delta_f": 3.0, "source": "history", "ref": None},
                {
                    "date": "2026-10-21",
                    "time": "18:00",
                    "bucket": EVENING,
                    "delta_f": 5.0,
                    "ref": None,
                    "rooms": {BED: 63.5, "thermostat": 66.0},
                },
                {"date": "2026-10-22", "time": "18:30", "bucket": EVENING, "delta_f": -1.0, "ref": None},
            ],
            "summary": {
                B4: {"n": 3, "median_f": 3.0, "offset_f": 3.0, "level": "band"},
                "cold|evening|weekday|-": {"n": 3, "median_f": 5.0, "offset_f": 5.0, "level": "band"},
            },
            "background": {"morning": {BED: [63.0, 64.0]}},
        }
    )


def test_accepting_the_seed_applies_offsets_now_within_the_cap(tmp_path):
    engine = _engine(tmp_path)
    engine.sample_room_background("morning", {BED: 65.0})
    _seed_state(engine)
    result = engine.accept_preference_seed(None, today=TODAY, refs={}, max_offset_f=3.6, rooms=(BED,))
    assert result == {"applied": [EVENING, B4], "skipped": [], "observations": 3}
    assert engine.preference_offsets() == {B4: 3.0, "cold|evening|weekday|-": 3.6}  # no 0.9 step; capped
    assert engine._state.room_background["morning"][BED] == [63.0, 64.0, 65.0]  # history before live
    assert engine._state.preference_seed is None
    assert engine._state.preference_history[-1]["kind"] == "seed_accepted"


def test_accepting_skips_unselected_rows_and_parts_steered_by_a_room(tmp_path):
    engine = _engine(tmp_path)
    engine.sample_room_background("morning", {BED: 65.0})
    _seed_state(engine)
    result = engine.accept_preference_seed([B4], today=TODAY, refs={"morning": BED}, max_offset_f=3.6, rooms=(BED,))
    assert result == {"applied": [], "skipped": [B4], "observations": 2}
    assert engine.preference_offsets() == {}
    obs = {o["time"]: o for o in engine._state.preference_observations}
    # The disabled row's observation is kept with its frame (ref null): it counts again if the morning returns to the
    # thermostat. The unticked evening raise is context: its room temperatures feed the learned room, never an offset.
    assert obs["07:00"]["ref"] is None and not obs["07:00"].get("context_only")
    assert obs["18:00"]["context_only"] is True and "18:30" not in obs  # a lower carries no room context
    assert engine._state.room_background["morning"][BED] == [63.0, 64.0, 65.0]  # background imported regardless
    assert engine.preference_summary()["observation_count"] == 1
    assert pl.observations_in_frame(engine._state.preference_observations, {}) == [obs["07:00"]]
    assert set(pl.raise_samples(engine._state.preference_observations, "evening")) == {BED, "thermostat"}


def test_accepting_twice_does_not_duplicate_observations(tmp_path):
    engine = _engine(tmp_path)
    _seed_state(engine)
    engine.accept_preference_seed(None, today=TODAY, refs={}, max_offset_f=3.6, rooms=(BED,))
    _seed_state(engine)
    again = engine.accept_preference_seed(None, today=TODAY, refs={}, max_offset_f=3.6, rooms=(BED,))
    assert again["observations"] == 0


def test_accepting_after_a_discard_turns_the_kept_context_into_observations(tmp_path):
    engine = _engine(tmp_path)
    _seed_state(engine)
    engine.discard_preference_seed(TODAY, rooms=(BED,))
    _seed_state(engine)
    engine.accept_preference_seed(None, today=TODAY, refs={}, max_offset_f=3.6, rooms=(BED,))
    assert not any(o.get("context_only") for o in engine._state.preference_observations)
    assert len(engine._state.preference_observations) == 3


def test_a_raise_imported_under_another_band_is_not_imported_again(tmp_path):
    # The same episode analysed twice with a different humidity source: other band, same episode start.
    engine = _engine(tmp_path)
    start = "2026-10-21T17:55:00+02:00"
    first = {"date": "2026-10-21", "time": "18:00", "start": start, "bucket": "cold|evening|weekday|-"}
    raise_ = {"delta_f": 5.0, "ref": None, "rooms": {BED: 63.5}, "source": "history"}
    engine._state.preference_observations = [{**first, **raise_, "context_only": True}]
    humid = "cold|evening|weekday|humid"
    engine.store_preference_seed(
        {
            "window": SEED_WINDOW,
            "counts": {},
            "observations": [{**first, "bucket": humid, **raise_}],
            "summary": {humid: {"n": 3, "median_f": 5.0, "offset_f": 3.0, "level": "band"}},
        }
    )
    result = engine.accept_preference_seed(None, today=TODAY, refs={}, max_offset_f=3.6, rooms=(BED,))
    assert result["observations"] == 0
    [kept] = engine._state.preference_observations
    assert kept["start"] == start and not kept.get("context_only")  # accepted now: it counts, once


def test_importing_a_seed_keeps_only_the_rooms_selected_now(tmp_path):
    gone = "sensor.study_temp"
    engine = _engine(tmp_path)
    engine.store_preference_seed(
        {
            "window": SEED_WINDOW,
            "counts": {},
            "observations": [
                {
                    "date": "2026-10-21",
                    "time": "18:00",
                    "bucket": EVENING,
                    "delta_f": 5.0,
                    "ref": None,
                    "rooms": {BED: 63.5, gone: 61.0, "thermostat": 66.0},
                },
                {
                    "date": "2026-10-22",
                    "time": "18:00",
                    "bucket": EVENING,
                    "delta_f": 4.0,
                    "ref": None,
                    "rooms": {gone: 61.0},
                },
            ],
            "summary": {},
            "background": {"morning": {BED: [63.0], gone: [60.0], "thermostat": [66.0]}},
        }
    )
    engine.discard_preference_seed(TODAY, rooms=(BED,))
    assert engine._state.room_background == {"morning": {BED: [63.0], "thermostat": [66.0]}}
    [context] = engine._state.preference_observations  # the raise with only the deselected room teaches nothing
    assert context["rooms"] == {BED: 63.5, "thermostat": 66.0}


def test_discarding_the_seed(tmp_path):
    engine = _engine(tmp_path)
    assert not engine.discard_preference_seed(TODAY, rooms=(BED,))
    _seed_state(engine)
    assert engine.discard_preference_seed(TODAY, rooms=(BED,))
    assert engine._state.preference_seed is None and engine.preference_offsets() == {}
    assert engine._state.preference_history[-1] == {"date": "2026-11-16", "kind": "seed_discarded"}
    # The room history stays as context: the background and the raise's room temperatures, no offset.
    assert engine._state.room_background == {"morning": {BED: [63.0, 64.0]}}
    assert [o["time"] for o in engine._state.preference_observations if o.get("context_only")] == ["18:00"]
    assert engine.preference_summary()["observation_count"] == 0
