"""Setpoint preference learning, phase 3: rooms, humidity and history backfill (fork Dunky13#1).

Pure parts only: humidity bands, four-part keys and the three-level weekly update, the steering reference, the
learned-room score, and the recorder backfill (schedule filter against the recorded Nest schedule of the target
install, history episodes, seed summary). Spec: docs/preference-learning-spec.md, "Phase 3".
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import preference_backfill as bf
from custom_components.climate_advisor import preference_learning as pl
from custom_components.climate_advisor.classifier import classify_day

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


def test_phase_one_three_part_observations_count_as_band_none():
    offsets, _ = pl.weekly_update(
        {}, _obs("cold|evening|weekday", 1.0, 1.0, 1.0), max_offset_f=3.6, step_f=3.6, today=TODAY
    )
    assert offsets["cold|evening|weekday|-"]["level"] == pl.LEVEL_BAND
    assert offsets["cold|evening|weekday"]["offset_f"] == 1.0


def test_seed_offsets_ignore_the_weekly_step_but_keep_the_cap():
    summary = pl.seed_offsets(_obs("cold|morning|weekday|-", 5.0, 6.0, 7.0), max_offset_f=3.6)
    assert summary["cold|morning|weekday|-"] == {"n": 3, "median_f": 6.0, "offset_f": 3.6, "level": "band"}
    small = pl.seed_offsets(_obs("cold|morning|weekday|-", 2.4, 2.5, 2.6), max_offset_f=3.6)
    assert small["cold|morning|weekday|-"]["offset_f"] == 2.5  # not limited to the 0.9 weekly step


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


def test_whole_series_is_judged_in_the_users_unit():
    assert pl.is_whole_series([_f(c) for c in (19, 20, 21, 20, 19)], "celsius")
    assert not pl.is_whole_series([_f(c) for c in (19, 20.5, 21, 20, 19)], "celsius")
    assert pl.is_whole_series([66.0, 67.0, 68.0, 67.0, 66.0], "fahrenheit")
    assert not pl.is_whole_series([66.0, 67.0], "fahrenheit")  # too few to tell


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


def _nest_history():
    """``(rows, truth)``: climate rows in UTC with setpoints in F, and per change whether it is the schedule."""
    events: list[tuple[datetime, float, bool]] = []
    for day in range(DAYS):
        for i, ((h, m), c) in enumerate(NEST_SCHEDULE):
            jitter = ((day + i) % 5) - 2  # -2..+2 minutes, never the same on consecutive days
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
    rows, _ = _nest_history()
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


def test_seed_skips_days_without_outdoor_data_and_stale_rooms():
    seed = _seed(outdoor=[], rooms={BED: [(_local(0, 0, 0).astimezone(UTC), 64.0)]})
    assert seed["observations"] == [] and seed["counts"]["no_outdoor"] > 0
    seed = _seed(rooms={BED: [(_local(0, 0, 0).astimezone(UTC), 64.0)]})
    assert BED not in seed["observations"][0]["rooms"]  # older than 30 minutes at the burst


def test_episode_exclusion_before_wake_applies_to_history():
    seed = _seed(config={**CONFIG, "wake_time": "07:00"})  # 06:40 is now within 60 minutes before wake
    assert all(o["time"] != "06:40" for o in seed["observations"])
