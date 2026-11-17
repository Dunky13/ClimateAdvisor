"""Which live state changes an automation caused (fork Dunky13#1; docs/preference-learning-spec.md).

One tracker and one predicate for the schedule-signal collector and the live preference observation path. A change is
automation-caused when its context has a ``parent_id``, or when a ``call_service`` event with a parent context targeted
the entity within the previous 120 seconds. The second rule exists because the Nest's state update arrives through the
cloud and often does not carry the caller's context. Targets resolve through entity, device and area ids.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

CALL_WINDOW = timedelta(seconds=120)


def targeted_entities(hass: HomeAssistant, service_data: dict[str, Any]) -> frozenset[str]:
    """Every entity a service call's data targets (entity ids, plus the entities of its devices and areas)."""
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(service_data))
    return frozenset(selected.referenced | selected.indirectly_referenced)


class AutomationCalls:
    """The recent service calls made under a parent context, with the entities each targeted."""

    def __init__(self) -> None:
        self._calls: deque[tuple[datetime, frozenset[str]]] = deque()

    def record(self, hass: HomeAssistant, event: Event) -> None:
        """Remember a ``call_service`` event that carries a parent context (event loop)."""
        service_data = event.data.get("service_data")
        if event.context.parent_id is None or not isinstance(service_data, dict):
            return
        self._prune(event.time_fired)
        self._calls.append((event.time_fired, targeted_entities(hass, service_data)))

    def caused(self, entity_id: str, context: Context | None, now: datetime) -> bool:
        """True when a state change of ``entity_id`` at ``now`` (UTC) was caused by an automation."""
        if context is not None and context.parent_id is not None:
            return True
        self._prune(now)
        return any(entity_id in targets for _, targets in self._calls)

    def _prune(self, now: datetime) -> None:
        while self._calls and now - self._calls[0][0] > CALL_WINDOW:
            self._calls.popleft()
