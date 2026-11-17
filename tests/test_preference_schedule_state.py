"""Learned wake and bed times, phase 2: the learning state (fork Dunky13#1; docs/preference-learning-spec.md).

Persistence and validation, the weekly update inside the phase 1 gate, live candidates, seed accept and discard.
"""

from __future__ import annotations

from datetime import date, timedelta

from custom_components.climate_advisor import preference_schedule as ps
from custom_components.climate_advisor.learning import LearningEngine

WINDOW = {"start": "2026-10-17T00:00:00+00:00", "end": "2026-11-16T00:00:00+00:00", "days": 30}
TODAY = date(2026, 11, 20)


def _engine(tmp_path):
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _cands(cls, kind, deltas, source="sensor", start=date(2026, 11, 1)):
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "class": cls, "kind": kind, "delta_min": d, "source": source}
        for i, d in enumerate(deltas)
    ]


def test_state_round_trips_and_a_fresh_engine_has_no_shift(tmp_path):
    engine = _engine(tmp_path)
    assert engine.schedule_shifts() == {"weekday": {"wake": 0, "bed": 0}, "weekend": {"wake": 0, "bed": 0}}
    engine._state.learned_schedule = {"weekday": {"wake": 35, "bed": 80}}
    engine._state.schedule_candidates = _cands("weekday", "wake", [33, 40])
    engine.save_state()
    reloaded = _engine(tmp_path)
    assert reloaded.schedule_shifts()["weekday"] == {"wake": 35, "bed": 80}
    assert len(reloaded.schedule_candidates()) == 2


def test_load_drops_malformed_shifts_and_candidates_with_a_warning(tmp_path, caplog):
    engine = _engine(tmp_path)
    engine._state.learned_schedule = {
        "weekday": {"wake": 35, "bed": "late", "nap": 5},
        "holiday": {"wake": 1},
        "weekend": "x",
    }
    good = _cands("weekend", "bed", [60])[0]
    engine._state.schedule_candidates = [
        good,
        {**good, "kind": "lunch"},
        {**good, "date": "soon"},
        {**good, "delta_min": 1.5},
        {**good, "delta_min": 900},
        {**good, "source": "guess"},
        "junk",
    ]
    engine.save_state()
    caplog.set_level("WARNING")
    reloaded = _engine(tmp_path)
    assert reloaded._state.learned_schedule == {"weekday": {"wake": 35}}
    assert reloaded.schedule_candidates() == [good]
    for what in ("learned schedule shift", "learned schedule class", "schedule candidates"):
        assert f"Preference state entry dropped: what={what} reason=" in caplog.text, what


def test_the_preferences_reset_scope_clears_the_schedule_state(tmp_path):
    engine = _engine(tmp_path)
    engine._state.learned_schedule = {"weekday": {"wake": 35}}
    engine._state.schedule_candidates = _cands("weekday", "wake", [33])
    engine.reset("preferences")
    assert engine._state.learned_schedule == {} and engine._state.schedule_candidates == []


def test_a_live_wake_keeps_the_first_signal_and_a_live_bed_the_last(tmp_path):
    engine = _engine(tmp_path)
    wake = {"date": "2026-11-20", "class": "weekday", "kind": "wake", "delta_min": 33}
    bed = {"date": "2026-11-21", "class": "weekend", "kind": "bed", "delta_min": 70}
    assert engine.add_schedule_candidate(wake, "sensor", today=TODAY)
    assert not engine.add_schedule_candidate({**wake, "delta_min": 50}, "sensor", today=TODAY)
    assert engine.add_schedule_candidate(bed, "sensor", today=TODAY)
    assert engine.add_schedule_candidate({**bed, "delta_min": 130}, "sensor", today=TODAY)
    assert {c["kind"]: c["delta_min"] for c in engine.schedule_candidates()} == {"wake": 33, "bed": 130}
    # The thermostat fallback never replaces: the first episode of the night stays.
    other = {**bed, "date": "2026-11-22"}
    assert engine.add_schedule_candidate(other, "thermostat", today=TODAY)
    assert not engine.add_schedule_candidate({**other, "delta_min": 90}, "thermostat", today=TODAY)


def test_the_weekly_update_moves_the_shift_one_step_inside_the_phase_one_gate(tmp_path, caplog):
    engine = _engine(tmp_path)
    engine._state.schedule_candidates = _cands("weekday", "wake", [60] * 5)
    caplog.set_level("INFO")
    changes = engine.run_preference_update(
        TODAY, max_offset_f=3.6, step_f=0.9, unit="celsius", max_shift_min=90, step_min=15
    )
    assert [c for c in changes if c.get("kind") == "schedule"][0]["new_min"] == 15
    assert engine.schedule_shifts()["weekday"]["wake"] == 15
    assert "Learned schedule changed: class=weekday signal=wake n=5 target=60min old=0min new=15min" in caplog.text
    assert engine._state.preference_history[-1]["kind"] == "schedule"
    # Not due again until a week has passed: the schedule update rides the same gate.
    assert (
        engine.run_preference_update(
            TODAY + timedelta(days=3), max_offset_f=3.6, step_f=0.9, unit="celsius", max_shift_min=90, step_min=15
        )
        is None
    )
    engine.run_preference_update(
        TODAY + timedelta(days=7), max_offset_f=3.6, step_f=0.9, unit="celsius", max_shift_min=90, step_min=15
    )
    assert engine.schedule_shifts()["weekday"]["wake"] == 30


def test_a_cap_of_zero_leaves_the_shifts_alone(tmp_path):
    engine = _engine(tmp_path)
    engine._state.schedule_candidates = _cands("weekday", "wake", [60] * 5)
    engine.run_preference_update(TODAY, max_offset_f=3.6, step_f=0.9, unit="celsius", max_shift_min=0, step_min=15)
    assert engine.schedule_shifts()["weekday"]["wake"] == 0


def _seed(rows=None, candidates=None):
    return {
        "window": WINDOW,
        "counts": {"observations": 0},
        "observations": [],
        "summary": {},
        "schedule": {
            "candidates": candidates
            if candidates is not None
            else _cands("weekday", "wake", [40, 44, 36, 38], "history")
            + _cands("weekend", "bed", [170, 160], "history"),
            "rows": rows
            if rows is not None
            else ps.seed_schedule_rows(
                _cands("weekday", "wake", [40, 44, 36, 38]) + _cands("weekend", "bed", [170, 160]), {}, max_shift_min=90
            ),
            "counts": {"transitions": 6, "schedule": 0, "outside": 0},
        },
    }


def test_accepting_a_seed_applies_the_selected_enabled_rows_now_and_adds_their_candidates(tmp_path, caplog):
    engine = _engine(tmp_path)
    engine.store_preference_seed(_seed())
    caplog.set_level("INFO")
    result = engine.accept_preference_seed(
        [],
        today=TODAY,
        refs={},
        max_offset_f=3.6,
        rooms=(),
        schedule_rows=["weekday|wake", "weekend|bed"],
        max_shift_min=30,
    )
    assert result["schedule"] == ["weekday|wake"]  # the weekend row has 2 candidates: disabled
    assert engine.schedule_shifts()["weekday"]["wake"] == 30  # clamped to the cap, not limited by the weekly step
    assert [c["delta_min"] for c in engine.schedule_candidates()] == [40, 44, 36, 38]
    assert engine._state.preference_seed is None
    assert "schedule_applied=['weekday|wake']" in caplog.text


def test_accepting_with_no_row_selected_applies_nothing_and_a_known_date_is_not_added_twice(tmp_path):
    engine = _engine(tmp_path)
    engine._state.schedule_candidates = _cands("weekday", "wake", [90], "sensor")  # 2026-11-01 already stored
    engine.store_preference_seed(_seed())
    result = engine.accept_preference_seed(
        None, today=TODAY, refs={}, max_offset_f=3.6, rooms=(), schedule_rows=[], max_shift_min=90
    )
    assert result["schedule"] == []
    assert engine.schedule_shifts()["weekday"]["wake"] == 0
    engine.store_preference_seed(_seed())
    engine.accept_preference_seed(
        None, today=TODAY, refs={}, max_offset_f=3.6, rooms=(), schedule_rows=None, max_shift_min=90
    )
    dates = [c["date"] for c in engine.schedule_candidates()]
    assert len(dates) == len(set(dates)) == 4  # 2026-11-01 kept once (the stored sensor candidate wins)
    assert next(c for c in engine.schedule_candidates() if c["date"] == "2026-11-01")["source"] == "sensor"


def test_discarding_a_seed_drops_its_schedule_candidates(tmp_path):
    engine = _engine(tmp_path)
    engine.store_preference_seed(_seed())
    assert engine.discard_preference_seed(TODAY, rooms=())
    assert engine.schedule_candidates() == [] and engine._state.preference_seed is None


def test_the_seed_summary_carries_the_schedule_rows_but_not_the_candidates(tmp_path):
    engine = _engine(tmp_path)
    engine.store_preference_seed(_seed())
    summary = engine.preference_summary()["seed"]["schedule"]
    assert set(summary) == {"rows", "counts"}
    assert summary["rows"]["weekday|wake"]["enabled"] is True


def test_a_malformed_seed_schedule_section_is_dropped_and_bad_rows_removed(tmp_path, caplog):
    engine = _engine(tmp_path)
    good = _seed()
    good["schedule"]["rows"]["weekday|nap"] = {"n": 1}
    good["schedule"]["candidates"].append({"date": "x"})
    caplog.set_level("WARNING")
    engine.store_preference_seed(good)
    assert set(engine._state.preference_seed["schedule"]["rows"]) == {"weekday|wake", "weekend|bed"}
    assert len(engine._state.preference_seed["schedule"]["candidates"]) == 6
    engine.store_preference_seed({**_seed(), "schedule": {"candidates": "x", "rows": {}}})
    assert "schedule" not in engine._state.preference_seed
    assert "what=history seed schedule reason=malformed" in caplog.text


def test_the_schedule_summary_lists_each_pair_with_its_evidence(tmp_path):
    engine = _engine(tmp_path)
    engine._state.learned_schedule = {"weekday": {"wake": 120}}
    engine._state.schedule_candidates = _cands("weekday", "wake", [40, 44, 36, 38])
    rows = {(r["class"], r["kind"]): r for r in engine.schedule_summary(90)["rows"]}
    wake = rows[("weekday", "wake")]
    assert (wake["shift_min"], wake["stored_shift_min"], wake["n"], wake["target_min"], wake["sources"]) == (
        90,
        120,
        4,
        40,
        ["sensor"],
    )
    assert rows[("weekend", "bed")]["target_min"] is None
