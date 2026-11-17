"""Which live state changes an automation caused (fork Dunky13#1; docs/preference-learning-spec.md).

One tracker and one predicate for the schedule-signal collector and the live preference observation path. A change is
automation-caused when its context has a ``parent_id``, when its context (or its parent) is the context of an
automation or script run seen in the previous 120 seconds, or when a ``call_service`` event under such a context
targeted the entity in that window. The run contexts are needed because a time-triggered automation's context has no
``parent_id`` (HA derives it only from the trigger's context), and the call-service rule because the Nest's state
update arrives through the cloud and often does not carry the caller's context. Targets resolve through entity,
device and area ids.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

CALL_WINDOW = timedelta(seconds=120)
RUN_EVENTS = ("automation_triggered", "script_started")
_MAX_RUNS = 1000  # bounds the memory a burst of runs can hold inside the window


def targeted_entities(hass: HomeAssistant, service_data: dict[str, Any]) -> frozenset[str]:
    """Every entity a service call's data targets (entity ids, plus the entities of its devices and areas)."""
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(service_data))
    return frozenset(selected.referenced | selected.indirectly_referenced)


class AutomationCalls:
    """The recent service calls made under a parent context, with the entities each targeted."""

    def __init__(self) -> None:
        self._calls: deque[tuple[datetime, frozenset[str]]] = deque()
        self._runs: dict[str, datetime] = {}  # context id of an automation or script run -> when it started

    def record_run(self, event: Event) -> None:
        """Remember the context of an ``automation_triggered`` or ``script_started`` event (event loop)."""
        self._prune(event.time_fired)
        self._runs[event.context.id] = event.time_fired
        while len(self._runs) > _MAX_RUNS:
            del self._runs[next(iter(self._runs))]

    def _is_run(self, context: Context | None) -> bool:
        if context is None:
            return False
        return context.parent_id is not None or context.id in self._runs or context.parent_id in self._runs

    def record(self, hass: HomeAssistant, event: Event) -> None:
        """Remember a ``call_service`` event made under an automation or script run (event loop)."""
        service_data = event.data.get("service_data")
        if not isinstance(service_data, dict) or not self._is_run(event.context):
            return
        self._prune(event.time_fired)
        self._calls.append((event.time_fired, targeted_entities(hass, service_data)))

    def caused(self, entity_id: str, context: Context | None, now: datetime) -> bool:
        """True when a state change of ``entity_id`` at ``now`` (UTC) was caused by an automation."""
        self._prune(now)
        if self._is_run(context):
            return True
        return any(entity_id in targets for _, targets in self._calls)

    def _prune(self, now: datetime) -> None:
        while self._calls and now - self._calls[0][0] > CALL_WINDOW:
            self._calls.popleft()
        for run in [r for r, at in self._runs.items() if now - at > CALL_WINDOW]:
            del self._runs[run]
