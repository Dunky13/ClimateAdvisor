"""Setpoint preference learning: the pure core (fork Dunky13#1, phases 1 and 3).

Occupant outcome: when the household keeps nudging the thermostat to the same temperature at the same
kind of time (a cold weekday evening, say), Climate Advisor learns that preference and arms it itself,
within safe bounds, so the nudging stops.

This module is pure (no Home Assistant, no clock): buckets, episode grouping, the weekly update and the
clamps. The coordinator owns the state and the wiring; ``automation.select_comfort_band`` is the one seam
that applies an offset to the heat floor. Heating only, internal degrees F throughout.

Phase 3 adds the steering reference (which room the comfort target is measured in, per part of day), the learned
room score, the humidity band as a fourth bucket part and the reference frame of each observation.

Spec: docs/preference-learning-spec.md.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from statistics import median
from typing import Any, NamedTuple

from .const import (
    CONF_ROOM_TEMP_ENTITIES,
    DEFAULT_PREFERENCE_MAX_OFFSET_F,
    REMOTE_CONTEXT_ROOM,
    REMOTE_CONTEXT_SLEEP,
    ROOM_PIN_AUTO,
    ROOM_PIN_KEYS,
    ROOM_PIN_THERMOSTAT,
)
from .preference_schedule import (
    DAY_CLASS_WEEKDAY,
    DAY_CLASS_WEEKEND,
    DAY_CLASSES,
    SOURCE_HISTORY,
    ScheduleView,
    clock_minutes,
    config_at,
    day_class,
    next_times,
)

PART_NIGHT = "night"
PART_MORNING = "morning"
PART_AFTERNOON = "afternoon"
PART_EVENING = "evening"
PARTS_OF_DAY: tuple[str, ...] = (PART_NIGHT, PART_MORNING, PART_AFTERNOON, PART_EVENING)
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

LEVEL_BAND = "band"  # the four-part key (phase 3)
LEVEL_BUCKET = "bucket"  # the band-agnostic three-part key
LEVEL_DAYTYPE_PART = "day_type_part"

# Phase 3: the humidity band of the reference room at episode open (relative humidity, %).
BAND_DRY = "dry"
BAND_NORMAL = "normal"
BAND_HUMID = "humid"
BAND_NONE = "-"  # no humidity known (and every phase 1 observation)
BANDS: tuple[str, ...] = (BAND_DRY, BAND_NORMAL, BAND_HUMID, BAND_NONE)
RH_DRY_BELOW = 40.0
RH_HUMID_ABOVE = 60.0
RH_HYSTERESIS = 2.0

# Phase 3: the learned room per part of day.
REF_THERMOSTAT = ROOM_PIN_THERMOSTAT  # the key the thermostat's own reading is scored under
RAISE_WINDOW = 60  # the most recent raise samples of a part of day that are scored
MIN_RAISE_SAMPLES = 10
MIN_BACKGROUND_SAMPLES = 10
BACKGROUND_RESERVOIR = 200
BACKGROUND_INTERVAL = timedelta(minutes=30)  # one background sample per room per 30-minute slot
MAD_FLOOR_F = 0.1
CHOOSE_MAX_RATIO = 0.7
CHOOSE_MIN_MARGIN = 0.15
SOURCE_SLEEP_SENSOR = "sleep_sensor"
SOURCE_PINNED = "pinned"
SOURCE_LEARNED = "learned"
SOURCE_THERMOSTAT = "thermostat"
EPISODE_CONTEXT_KEYS: tuple[str, ...] = ("ref", "rh", "band", "rooms")

_NOON_MIN = 12 * 60
_AFTERNOON_END_MIN = 17 * 60


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def part_of_day(at: datetime, wake_time: Any, sleep_time: Any, *, in_sleep_window: bool) -> tuple[str, date]:
    """``(part_of_day, day)`` of a local instant: the part and the calendar day its day class is read from.

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
    wake = clock_minutes(wake_time)
    sleep = clock_minutes(sleep_time)
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
    return part, day


def bucket_key(
    at: datetime,
    day_type: str,
    wake_time: Any,
    sleep_time: Any,
    *,
    in_sleep_window: bool,
    band: str | None = None,
    workdays: Mapping[date, bool] | None = None,
) -> str:
    """``"<day_type>|<part_of_day>|<day_class>"`` for a local instant: the single source for every caller.

    With ``band`` (phase 3) the key gains the humidity band as a fourth part: ``"<...>|<band>"``. See
    ``part_of_day`` for the part and ``preference_schedule.day_class`` for the day class (``workdays``: the resolved
    Workday mapping, phase 2). ``wake_time`` and ``sleep_time`` are the schedule at ``at`` (the caller's seam).
    """
    part, day = part_of_day(at, wake_time, sleep_time, in_sleep_window=in_sleep_window)
    key = f"{day_type}|{part}|{day_class(day, workdays)}"
    return key if band is None else f"{key}|{band}"


def near_schedule_edge(at: datetime, wake_time: Any, sleep_time: Any) -> bool:
    """True within 60 minutes before the wake time or before bedtime (midnight wrap handled).

    A pre-wake heat-up or a pre-bed lowering is a schedule-timing signal, not a temperature preference
    (spec: "Schedule-edge exclusion"), so an episode that starts here is never learned from.
    """
    minute = at.hour * 60 + at.minute
    for edge in (clock_minutes(wake_time), clock_minutes(sleep_time)):
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


def parse_bucket(key: Any) -> tuple[str, str, str, str | None] | None:
    """``(day_type, part_of_day, day_class, band)`` of a well-formed bucket key, else None (never raises).

    A three-part key (the band-agnostic level, and every phase 1 key) has band ``None``.
    """
    if not isinstance(key, str):
        return None
    parts = key.split("|")
    if len(parts) not in (3, 4) or not parts[0] or parts[1] not in PARTS_OF_DAY or parts[2] not in DAY_CLASSES:
        return None
    if len(parts) == 4 and parts[3] not in BANDS:
        return None
    return parts[0], parts[1], parts[2], (parts[3] if len(parts) == 4 else None)


def bucket_parts(key: str) -> tuple[str, str, str]:
    """``(day_type, part_of_day, day_class)`` of a bucket key (a four-part key's band is ignored)."""
    day_type, part, day_class = key.split("|")[:3]
    return day_type, part, day_class


def base_key(key: str) -> str:
    """The band-agnostic three-part key of a bucket key."""
    return "|".join(key.split("|")[:3])


def bucket_band(key: str) -> str | None:
    """A bucket key's humidity band; None for a three-part key (or a malformed one)."""
    parsed = parse_bucket(key)
    return None if parsed is None else parsed[3]


def with_band(key: str) -> str:
    """A bucket key as a four-part key: a three-part (phase 1) key gets band ``-``."""
    return key if bucket_band(key) is not None else f"{key}|{BAND_NONE}"


def seed_row_blocked(key: str, refs: Mapping[str, str | None]) -> bool:
    """True when a seed row's part of day is steered by a room now: the history (``ref`` null) is another frame."""
    return refs.get(bucket_parts(key)[1]) is not None


def humidity_band(rh: Any, current: str | None = None) -> str:
    """The humidity band of a relative humidity, with hysteresis against the band in force (``current``).

    ``dry`` below 40 %, ``normal`` 40 to 60, ``humid`` above 60, ``-`` when unknown. Leaving the band in force needs the
    reading to pass the edge by 2 % RH, so a reading hovering on 40 % does not flap between two buckets.
    """
    if not is_finite_number(rh) or not 0.0 <= float(rh) <= 100.0:
        return BAND_NONE
    value = float(rh)
    if current in (BAND_DRY, BAND_NORMAL, BAND_HUMID):
        dry_edge = RH_DRY_BELOW + (RH_HYSTERESIS if current == BAND_DRY else -RH_HYSTERESIS)
        humid_edge = RH_HUMID_ABOVE + (-RH_HYSTERESIS if current == BAND_HUMID else RH_HYSTERESIS)
    else:
        dry_edge, humid_edge = RH_DRY_BELOW, RH_HUMID_ABOVE
    if value < dry_edge:
        return BAND_DRY
    if value > humid_edge:
        return BAND_HUMID
    return BAND_NORMAL


def stored_offset(offsets: Mapping[str, Any], bucket: str) -> float:
    """A bucket's stored offset: the four-part key, else its band-agnostic three-part key, else 0 (unclamped).

    ``offsets`` maps a key to the offset or to its stored entry (``{"offset_f": ...}``).
    """
    raw = offsets.get(bucket)
    if raw is None and bucket_band(bucket) is not None:
        raw = offsets.get(base_key(bucket))
    if isinstance(raw, Mapping):
        raw = raw.get("offset_f")
    return float(raw or 0.0)


@dataclass(frozen=True)
class PreferenceView:
    """What the band resolver needs: the learned offsets and the cap, read-only.

    ``None`` in place of a view means "feature off": every offset is 0 and no output differs from upstream.
    Phase 3: ``band`` is the humidity band in force now (the four-part key's last part) and ``learned_rooms`` the
    learned room per part of day (``None`` = the thermostat). Both are snapshots (the view is read from executor
    threads), never live state.
    """

    offsets: Mapping[str, float] = field(default_factory=dict)
    max_offset_f: float = DEFAULT_PREFERENCE_MAX_OFFSET_F
    band: str = BAND_NONE
    learned_rooms: Mapping[str, str | None] = field(default_factory=dict)
    room_names: Mapping[str, str] = field(default_factory=dict)  # selected room -> friendly name (briefing text)
    schedule: ScheduleView = field(default_factory=ScheduleView)  # phase 2: learned wake and bed shifts, workdays

    def offset_for(self, bucket: str) -> float:
        """The offset for a bucket, clamped to the CURRENT cap (lowering the cap applies at once).

        A four-part key falls back to its band-agnostic three-part key, then 0.
        """
        return _clamp(stored_offset(self.offsets, bucket), self.max_offset_f)

    def offset_at(
        self, at: datetime, day_type: str, config: Mapping[str, Any], *, in_sleep_window: bool
    ) -> tuple[float, str]:
        """``(offset_f, bucket)`` for an instant under the schedule at that instant (four-part bucket)."""
        schedule = config_at(self.schedule, config, at)
        bucket = bucket_key(
            at,
            day_type,
            schedule.get("wake_time"),
            schedule.get("sleep_time"),
            in_sleep_window=in_sleep_window,
            band=self.band,
            workdays=self.schedule.workdays,
        )
        return self.offset_for(bucket), bucket


def config_for(config: dict, view: PreferenceView | None, at: datetime) -> dict:
    """``config`` with the wake and sleep times of the schedule at ``at``: the seam for callers without a coordinator.

    ``isinstance``: a stubbed engine or coordinator hands back a mock, which must read as "nothing learned".
    """
    return config_at(view.schedule, config, at) if isinstance(view, PreferenceView) else config


def learned_next(
    view: PreferenceView | None, config: dict, after: datetime, kind: str, default: datetime | None
) -> datetime | None:
    """The next learned wake or bedtime (``KIND_WAKE`` / ``KIND_BED``) after ``after``, else ``default``."""
    if not isinstance(view, PreferenceView) or not view.schedule.active:
        return default
    return next_times(view.schedule, config, after)[kind] or default


# --------------------------------------------------------------------------------------------------
# Steering reference (phase 3)
# --------------------------------------------------------------------------------------------------


class SteeringReference(NamedTuple):
    """The entity the comfort target is measured against at one moment.

    ``entity`` None = the thermostat (the primary reading). ``context`` is the ``IndoorTempReading.remote_context``
    the resolver sets when that entity's reading is used (``REMOTE_CONTEXT_SLEEP`` / ``REMOTE_CONTEXT_ROOM``).
    ``source``: ``sleep_sensor`` / ``pinned`` / ``learned`` / ``thermostat``.
    """

    entity: str | None
    context: str | None
    source: str
    part: str


def selected_rooms(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The room temperature sensors the user selected (a malformed value reads as none)."""
    rooms = config.get(CONF_ROOM_TEMP_ENTITIES)
    if not isinstance(rooms, (list, tuple)):
        return ()
    return tuple(r for r in rooms if isinstance(r, str) and r)


def room_pin(config: Mapping[str, Any], part: str) -> str:
    """The part of day's pin: ``auto``, ``thermostat`` or a selected room; a stale or bad pin reads as ``auto``."""
    pin = config.get(ROOM_PIN_KEYS.get(part, ""), ROOM_PIN_AUTO)
    if pin == ROOM_PIN_THERMOSTAT or (isinstance(pin, str) and pin in selected_rooms(config)):
        return pin
    return ROOM_PIN_AUTO


def part_reference(
    part: str, config: Mapping[str, Any], learned_rooms: Mapping[str, str | None] | None
) -> SteeringReference:
    """The reference for a part of day (see ``steering_reference``)."""
    sleep_entity = config.get("sleep_indoor_temp_entity")
    if part == PART_NIGHT and isinstance(sleep_entity, str) and sleep_entity:
        return SteeringReference(sleep_entity, REMOTE_CONTEXT_SLEEP, SOURCE_SLEEP_SENSOR, part)
    if learned_rooms is None:  # preference learning off: rooms never steer
        return SteeringReference(None, None, SOURCE_THERMOSTAT, part)
    pin = room_pin(config, part)
    if pin == ROOM_PIN_THERMOSTAT:
        return SteeringReference(None, None, SOURCE_PINNED, part)
    if pin != ROOM_PIN_AUTO:
        return SteeringReference(pin, REMOTE_CONTEXT_ROOM, SOURCE_PINNED, part)
    learned = learned_rooms.get(part)
    if isinstance(learned, str) and learned in selected_rooms(config):
        return SteeringReference(learned, REMOTE_CONTEXT_ROOM, SOURCE_LEARNED, part)
    return SteeringReference(None, None, SOURCE_THERMOSTAT, part)


def steering_reference(
    at: datetime,
    config: Mapping[str, Any],
    *,
    in_sleep_window: bool,
    learned_rooms: Mapping[str, str | None] | None,
) -> SteeringReference:
    """The entity whose temperature the comfort target is measured against at ``at``: the ONE source every caller uses.

    1. In the sleep window a configured ``sleep_indoor_temp_entity`` always wins (existing behaviour).
    2. Otherwise the part of day's pin: ``thermostat`` means none, a selected room means that room.
    3. ``auto`` uses the learned room for the part of day when one is confident, else the thermostat.

    ``learned_rooms`` None means preference learning is off: only rule 1 applies (identical to before phase 3).
    The caller falls back to the thermostat when the room's reading is not usable (the resolver does that).
    """
    part, _day = part_of_day(at, config.get("wake_time"), config.get("sleep_time"), in_sleep_window=in_sleep_window)
    return part_reference(part, config, learned_rooms)


def steering_room(
    at: datetime, config: Mapping[str, Any], *, in_sleep_window: bool, view: PreferenceView | None
) -> str | None:
    """The ``room_entity`` the indoor resolver reads now: the steering room, None when the thermostat or the sleep
    sensor is the reference (the resolver handles the sleep sensor itself) or the feature is off (``view`` None)."""
    if view is None:
        return None
    ref = steering_reference(
        at, config_for(config, view, at), in_sleep_window=in_sleep_window, learned_rooms=view.learned_rooms
    )
    return ref.entity if ref.context == REMOTE_CONTEXT_ROOM else None


def references_by_part(
    config: Mapping[str, Any], learned_rooms: Mapping[str, str | None] | None
) -> dict[str, str | None]:
    """``{part: reference entity or None}`` for every part of day: the frame each part's offsets belong to."""
    return {part: part_reference(part, config, learned_rooms).entity for part in PARTS_OF_DAY}


# --------------------------------------------------------------------------------------------------
# Learned room (phase 3)
# --------------------------------------------------------------------------------------------------


def mad(values: list[float]) -> float:
    """Median absolute deviation (0.0 for an empty list)."""
    if not values:
        return 0.0
    mid = median(values)
    return float(median(abs(v - mid) for v in values))


def is_whole_series(values_f: list[float], unit: str) -> bool:
    """True when every reading (5+) is a whole number in ``unit``, the unit the readings were taken in.

    The ``indoor_temp._is_whole`` rule applied in the unit a room sensor's state was read in (``temp_unit``: the
    reader converts every state from it). A sensor's native unit is not in its state: Home Assistant converts a
    whole-C sensor on a F system to 69.8, 71.6, so it is judged by those F readings (not whole), and a 0.1-C sensor
    reading 21.1 C (69.98 F) is never mistaken for a whole-F one. The tolerance covers the 0.01 F storage rounding.
    """
    if len(values_f) < 5:
        return False
    native = (lambda v: (v - 32.0) / 1.8) if unit == "celsius" else (lambda v: v)
    return all(abs(x - round(x)) < 0.01 for x in map(native, values_f))


def raise_samples(observations: list[dict], part: str) -> dict[str, list[float]]:
    """``{entity: [temp_f, ...]}`` from the most recent 60 raise observations of a part of day."""
    raises = [o for o in observations if isinstance(o.get("rooms"), dict) and bucket_parts(o["bucket"])[1] == part]
    out: dict[str, list[float]] = {}
    for obs in raises[-RAISE_WINDOW:]:
        for entity, temp in obs["rooms"].items():
            if is_finite_number(temp):
                out.setdefault(str(entity), []).append(float(temp))
    return out


def score_rooms(
    raises: Mapping[str, list[float]],
    background: Mapping[str, list[float]],
    *,
    whole: set[str] | frozenset[str] = frozenset(),
) -> tuple[str | None, dict[str, float], dict[str, int]]:
    """``(chosen room or None, {entity: ratio}, {entity: raise n})`` for one part of day.

    ``ratio = MAD(raise temps) / MAD(background temps)`` (MAD floor 0.1 F) for every entity with at least 10 raise and
    10 background samples, the thermostat (``REF_THERMOSTAT``) included. A low ratio means the occupants turn the heat
    up when that room reaches a consistent temperature, more consistently than the room normally varies. The room with
    the lowest ratio is chosen when it is at most 0.7 and at least 0.15 below the thermostat's ratio; a whole-degree
    sensor (``whole``) is scored but never chosen.
    """
    ratios: dict[str, float] = {}
    counts: dict[str, int] = {}
    for entity, values in raises.items():
        counts[entity] = len(values)
        bg = list(background.get(entity, ()))
        if len(values) < MIN_RAISE_SAMPLES or len(bg) < MIN_BACKGROUND_SAMPLES:
            continue
        ratios[entity] = round(max(mad(values), MAD_FLOOR_F) / max(mad(bg), MAD_FLOOR_F), 3)
    thermostat = ratios.get(REF_THERMOSTAT)
    if thermostat is None:
        return None, ratios, counts
    candidates = sorted((r, e) for e, r in ratios.items() if e != REF_THERMOSTAT and e not in whole)
    if not candidates:
        return None, ratios, counts
    ratio, entity = candidates[0]
    if ratio <= CHOOSE_MAX_RATIO and ratio <= thermostat - CHOOSE_MIN_MARGIN:
        return entity, ratios, counts
    return None, ratios, counts


def observations_in_frame(observations: list[dict], refs: Mapping[str, str | None]) -> list[dict]:
    """The observations recorded under the reference their part of day uses now (``ref`` missing reads as None).

    A ``context_only`` observation (history the user did not accept as offsets) never counts towards an offset.
    """
    return [
        o for o in observations if not o.get("context_only") and o.get("ref") == refs.get(bucket_parts(o["bucket"])[1])
    ]


def heat_gate_reasons(mode: Any, presets: Iterable[Any]) -> list[str]:
    """Why a setpoint change is not a heating-comfort change: ``[]`` when the mode is heat and no state is in eco.

    The one gate the live recorder and the history backfill share.
    """
    reasons = [] if str(mode) == "heat" else ["mode not heat"]
    if any(str(p or "").lower() == "eco" for p in presets):
        reasons.append("eco preset")
    return reasons


def episode_context(ref: str | None, band: str, rooms: Mapping[str, float], rh: Any) -> dict[str, Any]:
    """The phase 3 context an episode captures at open (see ``extend_episode``), ``rh`` rounded to 0.1 %."""
    context: dict[str, Any] = {"ref": ref, "band": band, "rooms": dict(rooms)}
    if is_finite_number(rh):
        context["rh"] = round(float(rh), 1)
    return context


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
    context: Mapping[str, Any] | None = None,
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

    ``context`` (phase 3) is what else is captured at open: ``ref`` (the reference entity, None = thermostat), ``rh``
    and ``band`` (the reference humidity) and ``rooms`` (``{entity: temp_f}``, ``thermostat`` for the primary reading).
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
        **{k: context[k] for k in EPISODE_CONTEXT_KEYS if context is not None and k in context},
    }, closed


def episode_expired(episode: dict, now: datetime) -> bool:
    """True once 15 minutes have passed since the episode's last change (it can no longer be extended)."""
    return now - datetime.fromisoformat(episode["last"]) > EPISODE_GAP


def close_episode(episode: dict) -> dict | None:
    """The episode's observation, or None when the occupant undid their change.

    ``delta_f = final setpoint - base target``; using the base (not the offset-adjusted target) makes the
    offset converge to the preference instead of accumulating. A final setpoint within 0.5 F of the
    offset-adjusted target the occupant started from is discarded.

    The phase 3 context captured at open is copied; the room temperatures (``rooms``) only for a raise
    (``final > old``): they are the learned room's raise samples.
    """
    if abs(float(episode["final_f"]) - float(episode["target_f"])) <= UNDO_TOLERANCE_F:
        return None
    last = datetime.fromisoformat(episode["last"])
    obs: dict[str, Any] = {
        "date": last.date().isoformat(),
        "time": f"{last.hour:02d}:{last.minute:02d}",
        "start": episode["start"],  # the episode's identity: the same change history always gives the same start
        "bucket": episode["bucket"],
        "delta_f": round(float(episode["final_f"]) - float(episode["base_f"]), 2),
    }
    for key in EPISODE_CONTEXT_KEYS:
        if key in episode and (key != "rooms" or float(episode["final_f"]) > float(episode["old_f"])):
            obs[key] = episode[key]
    return obs


def observation_identity(observation: Mapping[str, Any]) -> str:
    """One episode, whatever its bucket or band: its start, else (an observation from before 0.9.41.2) its close."""
    start = observation.get("start")
    return start if isinstance(start, str) else f"{observation.get('date')} {observation.get('time')}"


def live_since(
    observations: Iterable[Mapping[str, Any]], open_episode: Mapping[str, Any] | None, tz: tzinfo
) -> datetime | None:
    """The earliest moment live recording holds an episode for (the open one included); None without any.

    A history seed must not count those episodes again. An observation from before 0.9.41.2 has no ``start``; its
    close (``date`` + ``time`` in ``tz``) stands in, which the seed's 15-minute episode gap covers.
    """
    times = [datetime.fromisoformat(open_episode["start"])] if open_episode else []
    for o in observations:
        if o.get("source") == SOURCE_HISTORY:
            continue
        if is_aware_iso(o.get("start")):
            times.append(datetime.fromisoformat(o["start"]))
            continue
        try:
            times.append(datetime.combine(date.fromisoformat(o["date"]), time.fromisoformat(o["time"]), tzinfo=tz))
        except KeyError, TypeError, ValueError:
            continue
    return min(times, default=None)


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


def _sibling(key: str) -> str:
    """The same bucket in the other day class (a four-part key keeps its band)."""
    parts = key.split("|")
    parts[2] = DAY_CLASS_WEEKEND if parts[2] == DAY_CLASS_WEEKDAY else DAY_CLASS_WEEKDAY
    return "|".join(parts)


def _day_type_part(key: str) -> tuple[str, str]:
    day_type, part, _day_class = bucket_parts(key)
    return day_type, part


def _level_values(observations: list[dict]) -> dict[str, tuple[str, list[float]]]:
    """``{bucket: (level, newest 20 deltas)}`` for every bucket the weekly update sets (see ``weekly_update``)."""
    by_band: dict[str, list[float]] = {}
    by_base: dict[str, list[float]] = {}
    by_day_type_part: dict[tuple[str, str], list[float]] = {}
    for obs in observations:
        key, delta = with_band(str(obs["bucket"])), float(obs["delta_f"])
        by_band.setdefault(key, []).append(delta)
        by_base.setdefault(base_key(key), []).append(delta)
        by_day_type_part.setdefault(_day_type_part(key), []).append(delta)
    banded = {base_key(k) for k in by_band if bucket_band(k) != BAND_NONE}
    # A base with only band ``-`` observations: its ``-`` key would mirror the three-part key (same values), so only
    # the three-part key is kept (the lookup falls back to it); no duplicate row or history record.
    keys = {k for k in by_band if bucket_band(k) != BAND_NONE or base_key(k) in banded}
    out: dict[str, tuple[str, list[float]]] = {}
    for key in keys | set(by_base) | {_sibling(k) for k in by_base}:
        candidates: list[tuple[str, list[float]]] = []
        if bucket_band(key) is not None:
            candidates.append((LEVEL_BAND, by_band.get(key, [])))
        candidates.append((LEVEL_BUCKET, by_base.get(base_key(key), [])))
        candidates.append((LEVEL_DAYTYPE_PART, by_day_type_part.get(_day_type_part(key), [])))
        for level, values in candidates:
            if len(values) >= MIN_OBSERVATIONS:
                out[key] = (level, values[-RECENT_OBSERVATIONS:])
                break
    return out


def weekly_update(
    offsets: Mapping[str, Mapping[str, Any]],
    observations: list[dict],
    *,
    max_offset_f: float,
    step_f: float,
    today: date,
) -> tuple[dict[str, dict], list[dict]]:
    """One weekly pass: ``(new_offsets, changes)``; ``changes`` has one record per bucket whose offset moved.

    Observation buckets are four-part keys (a three-part phase 1 key reads as band ``-``). Offsets are computed
    for every four-part key with observations and for every three-part key with observations or a sibling (other
    day class) with observations. Each uses the most recent 20 observations of the first level that has at least
    3: the four-part key (four-part keys only), the band-agnostic three-part key across bands, then
    ``day_type|part_of_day`` across both day classes and every band (the phase 1 fallback); still fewer than 3
    means no change. The target is the median ``delta_f`` clamped to ``+-max_offset_f``; the offset moves toward
    it by at most ``step_f``, is clamped again and
    rounded to 0.1 F. A four-part key without an offset yet starts from its three-part key's offset (the value the
    lookup applied until then), so a new band does not drop a learned offset back towards 0. A base with only band
    ``-`` observations gets no ``-`` key (it would mirror the three-part key). The caller passes only the observations
    of the current reference frame.
    """
    new_offsets: dict[str, dict] = {k: dict(v) for k, v in offsets.items()}
    changes: list[dict] = []
    for key, (level, values) in sorted(_level_values(observations).items()):
        # A new four-part key starts from its three-part key's offset (what its lookup applied until now), not 0.
        current = stored_offset(offsets, key)
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


def drop_parts(offsets: Mapping[str, Any], parts: set[str] | frozenset[str]) -> dict[str, Any]:
    """The offsets without every bucket of the given parts of day (their reference changed: restart from 0)."""
    return {k: v for k, v in offsets.items() if bucket_parts(k)[1] not in parts}


def seed_offsets(observations: list[dict], *, max_offset_f: float) -> dict[str, dict[str, Any]]:
    """``{bucket: {"n", "median_f", "offset_f", "level"}}`` the seed would set: the weekly levels, no step limit.

    Accepting a seed is an explicit user decision, so its offset is the level's median clamped to the cap only.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, (level, values) in sorted(_level_values(observations).items()):
        mid = float(median(values))
        out[key] = {
            "n": len(values),
            "median_f": round(mid, 2),
            "offset_f": round(_clamp(mid, max_offset_f), OFFSET_ROUND_DIGITS),
            "level": level,
        }
    return out
