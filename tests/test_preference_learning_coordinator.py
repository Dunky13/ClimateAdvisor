"""Preference learning wiring (fork Dunky13#1): observation gating, and one band for chart, briefing and write."""

from __future__ import annotations

import types
from datetime import UTC, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import coordinator as coord_mod
from custom_components.climate_advisor.automation import select_comfort_band
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator, _compute_target_band_schedule
from custom_components.climate_advisor.preference_learning import PreferenceView
from tests.test_preference_learning import CONFIG, _classification

EVENING = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)  # Monday 18:00
B = "cold|evening|weekday"


def _state(mode="heat", preset=None):
    attrs = {} if preset is None else {"preset_mode": preset}
    return SimpleNamespace(state=mode, attributes=attrs)


def _coordinator(*, enabled=True, automation=True, occupancy="home", classified=True, offsets=None, remote_f=0.0):
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {**CONFIG, "preference_learning_enabled": enabled}
    coord.learning = MagicMock()
    coord.learning.record_preference_change.return_value = True
    coord.learning.preference_offsets.return_value = offsets if offsets is not None else {B: 2.0}
    coord._automation_enabled = automation
    coord._occupancy_mode = occupancy
    coord._current_classification = _classification() if classified else None
    coord._tou_bank_in_force = MagicMock(return_value=None)
    coord.automation_engine = SimpleNamespace(_remote_offset_applied_f=remote_f)
    return coord


def _record(coord, old_state=None, new_state=None, old_f=70.0, new_f=74.0):
    with patch.object(coord_mod.dt_util, "now", return_value=EVENING):
        return coord._record_preference_observation(old_state or _state(), new_state or _state(), old_f, new_f)


def test_a_heat_override_at_home_is_recorded_with_base_and_target():
    coord = _coordinator()
    assert _record(coord) is True
    kwargs = coord.learning.record_preference_change.call_args.kwargs
    assert kwargs["bucket"] == f"{B}|-"  # phase 3: the four-part key, band "-" with no humidity
    assert kwargs["base_f"] == 70.0  # the floor WITHOUT the learned offset
    assert kwargs["target_f"] == 72.0  # the floor the engine armed (offset included)
    assert (kwargs["old_f"], kwargs["new_f"]) == (70.0, 74.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": False},
        {"automation": False},
        {"occupancy": "away"},
        {"occupancy": "vacation"},
        {"classified": False},
    ],
)
def test_gates_that_block_an_observation(kwargs):
    coord = _coordinator(**kwargs)
    assert _record(coord) is False
    coord.learning.record_preference_change.assert_not_called()


def test_non_heat_mode_is_not_recorded():
    coord = _coordinator()
    assert _record(coord, new_state=_state("cool")) is False
    coord.learning.record_preference_change.assert_not_called()


@pytest.mark.parametrize("side", ["old", "new"])
def test_eco_preset_on_either_state_is_not_recorded(side):
    coord = _coordinator()
    eco = _state(preset="eco")
    kwargs = {"old_state": eco} if side == "old" else {"new_state": eco}
    assert _record(coord, **kwargs) is False
    coord.learning.record_preference_change.assert_not_called()


def test_guest_is_recorded():
    assert _record(_coordinator(occupancy="guest")) is True


def test_a_time_of_use_bank_holding_the_setpoint_is_not_recorded():
    coord = _coordinator()
    coord._tou_bank_in_force = MagicMock(return_value=object())
    assert _record(coord) is False


def test_missing_setpoints_are_not_recorded():
    assert _record(_coordinator(), old_f=None) is False


def test_preference_view_is_none_while_disabled_and_for_stubbed_config():
    assert _coordinator(enabled=False).preference_view() is None
    stub = object.__new__(ClimateAdvisorCoordinator)
    stub.config = MagicMock()
    assert stub.preference_view() is None
    assert object.__new__(ClimateAdvisorCoordinator).preference_view() is None


def test_preference_view_carries_offsets_and_cap():
    view = _coordinator().preference_view()
    assert isinstance(view, PreferenceView)
    assert view.offset_for(B) == 2.0
    assert view.max_offset_f == 3.6


def test_status_reports_enabled_bucket_and_offset():
    coord = _coordinator()
    coord.learning.preference_summary.return_value = {
        "buckets": [],
        "observation_count": 4,
        "last_update": "2026-11-09",
    }
    with patch.object(coord_mod.dt_util, "now", return_value=EVENING):
        status = coord.preference_status()
    assert status["enabled"] is True
    assert status["bucket"] == f"{B}|-"
    assert status["active_offset_f"] == 2.0
    assert status["observation_count"] == 4
    assert _coordinator(enabled=False).preference_status() == {"enabled": False}


def test_chart_band_briefing_and_written_setpoint_agree():
    """With an offset active, the chart band and the band every write uses are one number."""
    view = PreferenceView({B: 2.0, "cold|night|weekday": 1.5, "cold|afternoon|weekday": -1.0})
    c = _classification()
    config = {**CONFIG, "temp_unit": "fahrenheit"}
    with patch.object(coord_mod.dt_util, "as_local", side_effect=lambda x: x):
        for hour, asleep in ((15, False), (18, False), (2, True)):
            ts = datetime(2026, 11, 17 if asleep else 16, hour, 0, tzinfo=UTC)
            chart = _compute_target_band_schedule([ts], config, "home", ts, classification=c, preference_view=view)[0]
            written = select_comfort_band(
                c,
                config,
                occupancy_mode="home",
                in_sleep_window=asleep,
                aggressive_savings=False,
                at=ts,
                preferences=view,
            )
            assert chart["lower"] == pytest.approx(written.floor), hour
            assert chart["upper"] == pytest.approx(written.ceiling), hour


# ── briefing ────────────────────────────────────────────────────────────────────────────────────────────


def _tldr(preferences, now=datetime(2026, 11, 16, 18, 0, tzinfo=UTC)):
    from custom_components.climate_advisor.briefing import _generate_tldr_table

    config = {**CONFIG, "sleep_time": time(22, 30), "wake_time": time(6, 30), "temp_unit": "fahrenheit"}
    return _generate_tldr_table(_classification(), config, now=now, preferences=preferences)


def test_briefing_names_a_nonzero_learned_preference_and_quotes_the_shifted_floor():
    rows = _tldr(PreferenceView({B: 2.0}))
    table = "\n".join(rows)
    assert "Learned preference (Cold, weekday): evening +2.0°F" in table
    assert "72" in table  # the awake band quotes the floor the thermostat is armed with


def test_briefing_is_unchanged_with_the_feature_off_or_a_zero_offset():
    off = _tldr(None)
    assert not any("Learned preference" in row for row in off)
    assert _tldr(PreferenceView({B: 0.0})) == off
    assert _tldr(PreferenceView({})) == off


# ── API payload and config-flow display ─────────────────────────────────────────────────────────────────


def test_api_payload_converts_offsets_to_the_display_unit():
    from custom_components.climate_advisor.api import _preferences_payload

    coord = _coordinator(offsets={B: 1.8})
    coord.learning.preference_summary.return_value = {
        "buckets": [{"bucket": B, "offset_f": 1.8, "n": 5, "level": "bucket", "updated": "2026-11-09"}],
        "observation_count": 7,
        "last_update": "2026-11-09",
    }
    with patch.object(coord_mod.dt_util, "now", return_value=EVENING):
        payload = _preferences_payload(coord, "celsius")
    assert payload["enabled"] is True
    assert payload["active_offset"] == 1.0  # 1.8 F is a 1.0 C delta (scale only, no 32 offset)
    assert payload["buckets"][0]["offset"] == 1.0
    assert _preferences_payload(_coordinator(enabled=False), "celsius") == {"enabled": False}


def test_options_form_shows_learning_limits_in_the_users_unit_with_fine_steps():
    from custom_components.climate_advisor.config_flow import delta_field_display

    default, lo, hi, step = delta_field_display("preference_max_offset_f", None, True)
    assert (default, lo, hi) == (2.0, 0.0, 3.0)
    assert step == 0.1


# ── the real handler: CA's own writes never reach the observation hook ─────────────────────────────────


def _handler_coordinator(**stub_kwargs):
    from tests.test_heat_cool_override import _make_coordinator_stub

    coord = _make_coordinator_stub(classification=_classification(), **stub_kwargs)
    coord.config.update({**CONFIG, "preference_learning_enabled": True})
    coord.learning = MagicMock()
    coord.learning.preference_offsets.return_value = {B: 2.0}
    coord._automation_enabled = True
    coord._occupancy_mode = "home"
    coord._tou_bank_in_force = MagicMock(return_value=None)
    coord._executor_job = AsyncMock()
    coord.preference_view = types.MethodType(ClimateAdvisorCoordinator.preference_view, coord)
    coord._record_preference_observation = types.MethodType(
        ClimateAdvisorCoordinator._record_preference_observation, coord
    )
    return coord


def _drive(coord, event):
    import asyncio

    from tests.test_heat_cool_override import _PATCH_CALLBACK

    with (
        patch(_PATCH_CALLBACK, side_effect=lambda fn: fn),
        patch.object(coord_mod.dt_util, "now", return_value=EVENING),
    ):
        asyncio.run(coord._async_thermostat_changed(event))


def _heat_event(old=70.0, new=74.0):
    from tests.test_heat_cool_override import _make_event

    return _make_event(old_hvac_mode="heat", new_hvac_mode="heat", old_temp=old, new_temp=new)


def test_an_occupant_setpoint_change_reaches_the_hook_through_the_real_handler():
    coord = _handler_coordinator()
    _drive(coord, _heat_event())
    kwargs = coord.learning.record_preference_change.call_args.kwargs
    assert (kwargs["old_f"], kwargs["new_f"], kwargs["bucket"]) == (70.0, 74.0, f"{B}|-")
    assert (kwargs["base_f"], kwargs["target_f"]) == (70.0, 72.0)


@pytest.mark.parametrize(
    "case",
    ["temp_command_pending", "recent_temp_command", "recognised_echo", "holds_own_setpoint"],
)
def test_climate_advisors_own_writes_never_reach_the_hook(case):
    coord = _handler_coordinator()
    ae = coord.automation_engine
    if case == "temp_command_pending":
        ae._temp_command_pending = True
    elif case == "recent_temp_command":
        coord._is_recent_temp_command = MagicMock(return_value=True)
    elif case == "recognised_echo":
        ae.recognise_own_setpoint_echo = MagicMock(return_value=True)
    else:
        ae.holds_own_setpoint = MagicMock(return_value=True)
    _drive(coord, _heat_event())
    coord.learning.record_preference_change.assert_not_called()


def test_the_handler_skips_the_hook_while_the_feature_is_off():
    coord = _handler_coordinator()
    coord.config["preference_learning_enabled"] = False
    _drive(coord, _heat_event())
    coord.learning.record_preference_change.assert_not_called()


# ── remote-sensor offset, schedule edges, flush wiring, watchdog (review fixes) ──────────────────────────


def test_an_applied_remote_offset_is_not_learned_as_a_preference():
    # The thermostat holds target + 2 (remote offset): 72 -> 74 there is 70 -> 72 in the target frame.
    coord = _coordinator(remote_f=2.0)
    assert _record(coord, old_f=72.0, new_f=74.0) is True
    kwargs = coord.learning.record_preference_change.call_args.kwargs
    assert (kwargs["old_f"], kwargs["new_f"], kwargs["remote_f"]) == (72.0, 74.0, 2.0)


def test_a_schedule_edge_episode_is_flagged_excluded():
    coord = _coordinator()
    coord.config["wake_time"] = time(19, 0)  # EVENING is 18:00: inside the hour before wake
    _record(coord)
    assert coord.learning.record_preference_change.call_args.kwargs["excluded"] is True


def test_flush_closes_the_episode_when_enabled_and_discards_it_when_off():
    coord = _coordinator()
    coord.learning.flush_preference_episode.return_value = True
    with patch.object(coord_mod.dt_util, "now", return_value=EVENING):
        assert coord._flush_preference_episode() is True
    coord.learning.flush_preference_episode.assert_called_once_with(EVENING)
    off = _coordinator(enabled=False)
    off.learning.discard_preference_episode.return_value = True
    assert off._flush_preference_episode() is True
    off.learning.flush_preference_episode.assert_not_called()


def test_a_failing_flush_is_logged_and_swallowed(caplog):
    coord = _coordinator()
    coord.learning.flush_preference_episode.side_effect = RuntimeError("disk")
    with patch.object(coord_mod.dt_util, "now", return_value=EVENING), caplog.at_level("ERROR"):
        assert coord._flush_preference_episode() is False
    assert "Preference episode flush failed" in caplog.text


def test_weekly_update_discards_when_off_and_swallows_failures(caplog):
    off = _coordinator(enabled=False)
    off.learning.discard_preference_episode.return_value = False
    assert off._run_preference_update(EVENING) is False
    off.learning.run_preference_update.assert_not_called()
    on = _coordinator()
    on.learning.run_preference_update.side_effect = ValueError("corrupt")
    with caplog.at_level("ERROR"):
        assert on._run_preference_update(EVENING) is False
    assert "Preference weekly update failed" in caplog.text


@pytest.mark.parametrize("offset", [0.0, 1.8, -1.8])
def test_the_promises_judge_against_the_floor_the_thermostat_was_armed_with(offset):
    from custom_components.climate_advisor.invariant_watchdog import PromiseConfig

    cfg = PromiseConfig.from_config(
        {"sleep_time": "22:00", "wake_time": "06:00", "comfort_heat": 70, "sleep_heat": 64, "comfort_cool": 74},
        heat_offset_at=lambda at, asleep: offset,
    )
    noon = datetime(2026, 11, 16, 12, 0)
    night = datetime(2026, 11, 16, 2, 0)
    assert cfg.floor_lo(noon) == pytest.approx(70 + offset)
    assert cfg.floor_hi(noon) == pytest.approx(70 + offset)
    assert cfg.floor_lo(night) == pytest.approx(64 + offset)


def test_the_promise_floor_never_passes_its_ceiling():
    from custom_components.climate_advisor.invariant_watchdog import PromiseConfig

    cfg = PromiseConfig.from_config(
        {"comfort_heat": 70, "comfort_cool": 74, "sleep_time": "22:00", "wake_time": "06:00"},
        heat_offset_at=lambda at, asleep: 9.0,
    )
    assert cfg.floor_lo(datetime(2026, 11, 16, 12, 0)) == 74


@pytest.mark.parametrize(
    ("day", "evening", "night", "expected"),
    [
        # Friday evening is weekday, Friday night wakes into Saturday: the night entry names its own class.
        (20, "cold|evening|weekday", "cold|night|weekend", "evening +2.0°F and tonight (weekend) +1.0°F"),
        # Sunday evening is weekend, Sunday night wakes into Monday.
        (22, "cold|evening|weekend", "cold|night|weekday", "evening +2.0°F and tonight (weekday) +1.0°F"),
        # Monday night stays in the header's class: no repeated label.
        (16, "cold|evening|weekday", "cold|night|weekday", "evening +2.0°F and tonight +1.0°F"),
    ],
)
def test_each_listed_offset_carries_its_own_day_class(day, evening, night, expected):
    now = datetime(2026, 11, day, 18, 0, tzinfo=UTC)
    table = "\n".join(_tldr(PreferenceView({evening: 2.0, night: 1.0}), now))
    assert expected in table
