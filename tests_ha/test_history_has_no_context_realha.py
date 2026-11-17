"""Real-HA test: the recorder's history carries no context (fork #1 phase 2; docs/preference-learning-spec.md).

The history backfill cannot tell a person's thermostat or light change from an automation's: its states come back with
an empty context, whatever context wrote them. If a future Home Assistant starts returning the context, this fails and
the backfill can use it instead of the schedule filter.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.components import recorder
from homeassistant.components.recorder import history
from homeassistant.core import Context
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

LIGHT = "light.bedroom"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Override the suite's autouse fixture: it sets ``hass`` up first, and the recorder must be set up before it."""


async def test_history_states_have_an_empty_context_whatever_wrote_them(recorder_mock, hass):
    start = dt_util.utcnow() - timedelta(minutes=1)
    hass.states.async_set(LIGHT, "off")
    hass.states.async_set(LIGHT, "on", context=Context(user_id="person-1"))
    hass.states.async_set(LIGHT, "off", context=Context(parent_id="automation-run-1"))
    live = hass.states.get(LIGHT)
    assert live.context.parent_id == "automation-run-1", "the live state carries the context"
    await async_wait_recording_done(hass)

    states = await recorder.get_instance(hass).async_add_executor_job(
        lambda: history.get_significant_states(hass, start, dt_util.utcnow(), [LIGHT], significant_changes_only=False)
    )

    rows = states[LIGHT]
    assert [s.state for s in rows] == ["off", "on", "off"]
    assert all(s.context.parent_id is None and s.context.user_id is None for s in rows)
