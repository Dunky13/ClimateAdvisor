"""Setpoint preference learning: the pure core (fork Dunky13#1, phase 1).

Occupant outcome: when the household keeps nudging the thermostat to the same temperature at the same
kind of time (a cold weekday evening, say), Climate Advisor learns that preference and arms it itself,
within safe bounds, so the nudging stops.

This module is pure (no Home Assistant, no clock): buckets, episode grouping, the weekly update and the
clamps. The coordinator owns the state and the wiring; ``automation.select_comfort_band`` is the one seam
that applies an offset to the heat floor. Heating only, internal degrees F throughout.

Spec: docs/preference-learning-spec.md.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from .const import DEFAULT_PREFERENCE_MAX_OFFSET_F

PART_NIGHT = "night"
PART_MORNING = "morning"
PART_AFTERNOON = "afternoon"
PART_EVENING = "evening"
PARTS_OF_DAY: tuple[str, ...] = (PART_NIGHT, PART_MORNING, PART_AFTERNOON, PART_EVENING)
DAY_CLASS_WEEKDAY = "weekday"
DAY_CLASS_WEEKEND = "weekend"
DAY_CLASSES: tuple[str, ...] = (DAY_CLASS_WEEKDAY, DAY_CLASS_WEEKEND)

EPISODE_GAP = timedelta(minutes=15)
SCHEDULE_EDGE_MINUTES = 60
UNDO_TOLERANCE_F = 0.5
OBSERVATION_RETENTION_DAYS = 400
MAX_OBSERVATIONS = 2000
MAX_HISTORY = 200
RECENT_OBSERVATIONS = 20
MIN_OBSERVATIONS = 3
UPDATE_INTERVAL_DAYS = 7
OFFSET_ROUND_DIGITS = 1

LEVEL_BUCKET = "bucket"
LEVEL_DAYTYPE_PART = "day_type_part"

_NOON_MIN = 12 * 60
_AFTERNOON_END_MIN = 17 * 60


def _minutes(value: Any) -> int | None:
    """``"HH:MM"`` / ``"HH:MM:SS"`` as minutes after midnight, else None (unset or malformed)."""
    try:
        parts = str(value).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError, IndexError, TypeError:
        return None


def bucket_key(
    at: datetime,
    day_type: str,
    wake_time: Any,
    sleep_time: Any,
    *,
    in_sleep_window: bool,
) -> str:
    """``"<day_type>|<part_of_day>|<day_class>"`` for a local instant: the single source for every caller.

    ``in_sleep_window`` is the resolver's own verdict (``automation._in_sleep_window``, or the forced value
    bedtime/pre-cool/wake pass), so the night part can never disagree with the sleep branch that applies it.
    A night belongs to the day it wakes into: before the wake time that is the same date, from the wake time
    on it is the next date (Friday night is ``weekend``, Sunday night ``weekday``). Awake hours before noon
    are ``morning``, to 17:00 ``afternoon``, then ``evening`` until bedtime; a wake time at or after noon
    leaves the morning empty (the same clipping on the other edges falls out of the sleep-window precedence).
    A night-owl's awake hours after midnight (before a bedtime that is itself after midnight) are the
    previous day's ``evening``.
    """
    minute = at.hour * 60 + at.minute
    wake = _minutes(wake_time)
    sleep = _minutes(sleep_time)
    day = at.date()
    if in_sleep_window:
        part = PART_NIGHT
        if wake is not None and minute >= wake:
            day += timedelta(days=1)
    elif sleep is not None and wake is not None and sleep < wake and minute < sleep:
        part = PART_EVENING
        day -= timedelta(days=1)
    elif minute < _NOON_MIN:
        part = PART_MORNING
    elif minute < _AFTERNOON_END_MIN:
        part = PART_AFTERNOON
    else:
        part = PART_EVENING
    day_class = DAY_CLASS_WEEKEND if day.weekday() >= 5 else DAY_CLASS_WEEKDAY
    return f"{day_type}|{part}|{day_class}"


def near_schedule_edge(at: datetime, wake_time: Any, sleep_time: Any) -> bool:
    """True within 60 minutes before the wake time or before bedtime (midnight wrap handled).

    A pre-wake heat-up or a pre-bed lowering is a schedule-timing signal, not a temperature preference
    (spec: "Schedule-edge exclusion"), so an episode that starts here is never learned from.
    """
    minute = at.hour * 60 + at.minute
    for edge in (_minutes(wake_time), _minutes(sleep_time)):
        if edge is not None and (edge - minute) % (24 * 60) <= SCHEDULE_EDGE_MINUTES:
            return True
    return False


def is_finite_number(value: Any) -> bool:
    """A real, finite int or float (bool, NaN and Inf are not numbers here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_aware_iso(value: Any) -> bool:
    """A timezone-aware ISO-8601 timestamp string (a naive one would break the aware-vs-naive comparisons)."""
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def is_iso_date(value: Any) -> bool:
    """A ``YYYY-MM-DD`` string."""
    try:
        return isinstance(value, str) and bool(date.fromisoformat(value))
    except ValueError:
        return False


def parse_bucket(key: Any) -> tuple[str, str, str] | None:
    """``(day_type, part_of_day, day_class)`` of a well-formed bucket key, else None (never raises)."""
    if not isinstance(key, str):
        return None
    parts = key.split("|")
    if len(parts) != 3 or not parts[0] or parts[1] not in PARTS_OF_DAY or parts[2] not in DAY_CLASSES:
        return None
    return parts[0], parts[1], parts[2]


def bucket_parts(key: str) -> tuple[str, str, str]:
    """``(day_type, part_of_day, day_class)`` of a bucket key."""
    day_type, part, day_class = key.split("|")
    return day_type, part, day_class


@dataclass(frozen=True)
class PreferenceView:
    """What the band resolver needs: the learned offsets and the cap, read-only.

    ``None`` in place of a view means "feature off": every offset is 0 and no output differs from upstream.
    """

    offsets: Mapping[str, float] = field(default_factory=dict)
    max_offset_f: float = DEFAULT_PREFERENCE_MAX_OFFSET_F

    def offset_for(self, bucket: str) -> float:
        """The offset for a bucket, clamped to the CURRENT cap (lowering the cap applies at once)."""
        raw = self.offsets.get(bucket, 0.0)
        return max(-self.max_offset_f, min(self.max_offset_f, float(raw)))

    def offset_at(
        self, at: datetime, day_type: str, config: Mapping[str, Any], *, in_sleep_window: bool
    ) -> tuple[float, str]:
        """``(offset_f, bucket)`` for an instant under ``config``'s wake and sleep times."""
        bucket = bucket_key(
            at, day_type, config.get("wake_time"), config.get("sleep_time"), in_sleep_window=in_sleep_window
        )
        return self.offset_for(bucket), bucket


# --------------------------------------------------------------------------------------------------
# Episodes
# --------------------------------------------------------------------------------------------------


def extend_episode(
    episode: dict | None,
    *,
    at: datetime,
    old_f: float,
    new_f: float,
    bucket: str,
    base_f: float,
    target_f: float,
    excluded: bool = False,
    remote_f: float = 0.0,
) -> tuple[dict | None, dict | None]:
    """Fold one occupant setpoint change into the open episode: ``(open_episode, closed_observation)``.

    A change within 15 minutes of the previous one extends the episode (its final setpoint is the one that
    counts). A later change first closes the previous episode (returned as an observation, or None when it was
    discarded) and opens a new one. ``base_f`` is CA's heat floor WITHOUT any learned offset and ``target_f``
    the offset-adjusted floor, both for the moment the episode starts; the bucket is keyed there too.
    ``excluded`` (a start inside the schedule-edge window) stops a NEW episode from opening; extending an
    episode that started outside the window is unaffected.

    ``old_f`` / ``new_f`` are the setpoints the thermostat reported. ``remote_f`` is the signed remote-sensor offset
    its setpoint carried when the episode OPENED; it is stored in the episode and applied to every change in it, because
    the offset stops being applied once the occupant takes the thermostat (a later change must not read it as 0).
    """
    closed: dict | None = None
    if episode is not None:
        last = datetime.fromisoformat(episode["last"])
        if at - last <= EPISODE_GAP:
            return {**episode, "last": at.isoformat(), "final_f": new_f - float(episode.get("remote_f", 0.0))}, None
        closed = close_episode(episode)
    if excluded:
        return None, closed
    return {
        "start": at.isoformat(),
        "last": at.isoformat(),
        "bucket": bucket,
        "base_f": base_f,
        "target_f": target_f,
        "old_f": old_f - remote_f,
        "final_f": new_f - remote_f,
        "remote_f": remote_f,
    }, closed


def episode_expired(episode: dict, now: datetime) -> bool:
    """True once 15 minutes have passed since the episode's last change (it can no longer be extended)."""
    return now - datetime.fromisoformat(episode["last"]) > EPISODE_GAP


def close_episode(episode: dict) -> dict | None:
    """The episode's observation, or None when the occupant undid their change.

    ``delta_f = final setpoint - base target``; using the base (not the offset-adjusted target) makes the
    offset converge to the preference instead of accumulating. A final setpoint within 0.5 F of the
    offset-adjusted target the occupant started from is discarded.
    """
    if abs(float(episode["final_f"]) - float(episode["target_f"])) <= UNDO_TOLERANCE_F:
        return None
    last = datetime.fromisoformat(episode["last"])
    return {
        "date": last.date().isoformat(),
        "time": f"{last.hour:02d}:{last.minute:02d}",
        "bucket": episode["bucket"],
        "delta_f": round(float(episode["final_f"]) - float(episode["base_f"]), 2),
    }


def trim_observations(observations: list[dict], today: date) -> list[dict]:
    """Keep 400 days and at most 2000 entries (oldest dropped first)."""
    cutoff = (today - timedelta(days=OBSERVATION_RETENTION_DAYS)).isoformat()
    kept = [o for o in observations if str(o.get("date", "")) >= cutoff]
    return kept[-MAX_OBSERVATIONS:]


# --------------------------------------------------------------------------------------------------
# Weekly update
# --------------------------------------------------------------------------------------------------


def update_due(last_update: str | None, today: date) -> bool:
    """True when no update has run yet or at least 7 days have passed since the previous one."""
    if not last_update:
        return True
    try:
        return (today - date.fromisoformat(last_update)).days >= UPDATE_INTERVAL_DAYS
    except ValueError:
        return True


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _sibling(key: str) -> str:
    day_type, part, day_class = bucket_parts(key)
    other = DAY_CLASS_WEEKEND if day_class == DAY_CLASS_WEEKDAY else DAY_CLASS_WEEKDAY
    return f"{day_type}|{part}|{other}"


def weekly_update(
    offsets: Mapping[str, Mapping[str, Any]],
    observations: list[dict],
    *,
    max_offset_f: float,
    step_f: float,
    today: date,
) -> tuple[dict[str, dict], list[dict]]:
    """One weekly pass: ``(new_offsets, changes)``; ``changes`` has one record per bucket whose offset moved.

    Per bucket: the most recent 20 observations (any age within retention); fewer than 3 falls back to the
    bucket's ``day_type|part_of_day`` across both day classes, still fewer than 3 means no change. The target
    is the median ``delta_f`` clamped to ``+-max_offset_f``; the offset moves toward it by at most ``step_f``,
    is clamped again and rounded to 0.1 F.
    """
    by_bucket: dict[str, list[float]] = {}
    for obs in observations:
        by_bucket.setdefault(str(obs["bucket"]), []).append(float(obs["delta_f"]))
    buckets = set(by_bucket) | {_sibling(k) for k in by_bucket}

    new_offsets: dict[str, dict] = {k: dict(v) for k, v in offsets.items()}
    changes: list[dict] = []
    for key in sorted(buckets):
        values = by_bucket.get(key, [])[-RECENT_OBSERVATIONS:]
        level = LEVEL_BUCKET
        if len(values) < MIN_OBSERVATIONS:
            # Both day classes of day_type|part_of_day, the newest 20 of the pooled observations.
            level = LEVEL_DAYTYPE_PART
            values = [float(o["delta_f"]) for o in observations if str(o["bucket"]) in (key, _sibling(key))][
                -RECENT_OBSERVATIONS:
            ]
        if len(values) < MIN_OBSERVATIONS:
            continue
        current = float(offsets.get(key, {}).get("offset_f", 0.0))
        target = _clamp(float(median(values)), max_offset_f)
        new = _clamp(current + _clamp(target - current, step_f), max_offset_f)
        new = round(new, OFFSET_ROUND_DIGITS)
        new_offsets[key] = {"offset_f": new, "n": len(values), "level": level, "updated": today.isoformat()}
        if new != round(current, OFFSET_ROUND_DIGITS):
            changes.append(
                {
                    "date": today.isoformat(),
                    "bucket": key,
                    "level": level,
                    "n": len(values),
                    "target_f": round(target, 2),
                    "old_f": round(current, 2),
                    "new_f": new,
                }
            )
    return new_offsets, changes
