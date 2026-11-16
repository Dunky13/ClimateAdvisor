"""Preference learning history backfill: the pure part (fork Dunky13#1, phase 3).

Turns recorder history (already read off the event loop and converted to degrees F) into a ``preference_seed``: the
occupant's setpoint episodes with the thermostat's own schedule removed, bucketed like live observations, plus a
per-bucket summary of the offsets they would set. Nothing here touches Home Assistant; the coordinator reads the
recorder and passes plain rows.

Spec: docs/preference-learning-spec.md, "History backfill and seed".
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from .classifier import DayClassification, ForecastSnapshot
from .preference_learning import (
    BACKGROUND_RESERVOIR,
    BAND_NONE,
    REF_THERMOSTAT,
    SOURCE_HISTORY,
    bucket_key,
    close_episode,
    extend_episode,
    humidity_band,
    in_sleep_window,
    near_schedule_edge,
    part_of_day,
    seed_offsets,
)

SCHEDULE_TOLERANCE_F = 0.1 * 1.8 + 1e-6  # the same setpoint: within 0.1 C
SCHEDULE_WINDOW_MIN = 10  # the same clock time: within 10 minutes (wrapping midnight)
SCHEDULE_MIN_SHARE = 0.4  # of the days the history covers
SCHEDULE_MIN_DAYS = 4
ROOM_MAX_AGE = timedelta(minutes=30)
BACKGROUND_INTERVAL = timedelta(minutes=30)
_DAY_MIN = 24 * 60

Series = list[tuple[datetime, float]]


@dataclass(frozen=True)
class ClimateRow:
    """One recorded state of the climate entity (setpoint and reading in degrees F)."""

    at: datetime
    mode: str
    setpoint_f: float | None
    preset: str | None = None
    current_f: float | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class SetpointChange:
    """A setpoint change made in heat mode outside the eco preset."""

    at: datetime
    old_f: float
    new_f: float
    user_id: str | None = None


def setpoint_changes(rows: list[ClimateRow]) -> list[SetpointChange]:
    """Every heat-mode setpoint change (neither state in the eco preset), oldest first."""
    out: list[SetpointChange] = []
    prev: ClimateRow | None = None
    for row in sorted(rows, key=lambda r: r.at):
        if (
            prev is not None
            and prev.setpoint_f is not None
            and row.setpoint_f is not None
            and abs(row.setpoint_f - prev.setpoint_f) > 1e-6
            and row.mode == "heat"
            and "eco" not in (str(prev.preset or "").lower(), str(row.preset or "").lower())
        ):
            out.append(SetpointChange(row.at, prev.setpoint_f, row.setpoint_f, row.user_id))
        prev = row
    return out


def _clock_gap(a: datetime, b: datetime) -> int:
    d = abs((a.hour * 60 + a.minute) - (b.hour * 60 + b.minute))
    return min(d, _DAY_MIN - d)


def schedule_mask(changes: list[SetpointChange], tz: tzinfo, days_covered: int) -> list[bool]:
    """True for each change that is the thermostat's schedule.

    A change is the schedule when the same setpoint (+-0.1 C) is set within +-10 minutes of the same local clock
    time on at least 40 % of the days the history covers and on at least 4 days. A change made in Home Assistant by a
    person (context ``user_id``) is never the schedule.
    """
    local = [c.at.astimezone(tz) for c in changes]
    need = max(SCHEDULE_MIN_DAYS, math.ceil(SCHEDULE_MIN_SHARE * days_covered))
    mask: list[bool] = []
    for i, change in enumerate(changes):
        if change.user_id:
            mask.append(False)
            continue
        days = {
            local[j].date()
            for j, other in enumerate(changes)
            if abs(other.new_f - change.new_f) <= SCHEDULE_TOLERANCE_F
            and _clock_gap(local[i], local[j]) <= SCHEDULE_WINDOW_MIN
        }
        mask.append(len(days) >= need)
    return mask


class _Lookup:
    """A time series sorted for "the newest value at or before" lookups."""

    def __init__(self, series: Series) -> None:
        ordered = sorted(series, key=lambda p: p[0])
        self.times = [t for t, _ in ordered]
        self.values = [v for _, v in ordered]

    def at(self, at: datetime, max_age: timedelta | None = None) -> float | None:
        """None when there is no value at or before ``at``, or it is older than ``max_age``."""
        i = bisect_right(self.times, at)
        if i == 0 or (max_age is not None and at - self.times[i - 1] > max_age):
            return None
        return self.values[i - 1]


def _band_series(humidity: Series) -> list[tuple[datetime, str]]:
    """The humidity band in force after each reading (hysteresis against the band before it)."""
    out: list[tuple[datetime, str]] = []
    band: str | None = None
    for when, rh in humidity:
        band = humidity_band(rh, band)
        out.append((when, band))
    return out


def _daily_outdoor(outdoor: Series, tz: tzinfo) -> dict[Any, tuple[float, float]]:
    """``{local date: (high_f, low_f)}``."""
    days: dict[Any, list[float]] = {}
    for when, temp in outdoor:
        days.setdefault(when.astimezone(tz).date(), []).append(temp)
    return {d: (max(v), min(v)) for d, v in days.items()}


def background_samples(
    rooms: Mapping[str, Series],
    *,
    start: datetime,
    end: datetime,
    tz: tzinfo,
    config: Mapping[str, Any],
) -> dict[str, dict[str, list[float]]]:
    """``{part: {entity: [temp_f, ...]}}``: each entity's reading every 30 minutes, newest 200 per part.

    The same sampling the live 30-minute refresh does, replayed over the history window.
    """
    wake, sleep = config.get("wake_time"), config.get("sleep_time")
    lookups = {entity: _Lookup(series) for entity, series in rooms.items()}
    out: dict[str, dict[str, list[float]]] = {}
    at = start
    while at <= end:
        loc = at.astimezone(tz)
        part, _day = part_of_day(loc, wake, sleep, in_sleep_window=in_sleep_window(loc, sleep, wake))
        for entity, lookup in lookups.items():
            value = lookup.at(at, ROOM_MAX_AGE)
            if value is not None:
                out.setdefault(part, {}).setdefault(entity, []).append(round(value, 2))
        at += BACKGROUND_INTERVAL
    return {p: {e: v[-BACKGROUND_RESERVOIR:] for e, v in ents.items()} for p, ents in out.items()}


def build_seed(
    climate: list[ClimateRow],
    *,
    rooms: Mapping[str, Series],
    humidity: Series,
    outdoor: Series,
    start: datetime,
    end: datetime,
    tz: tzinfo,
    config: Mapping[str, Any],
    classify: Callable[[ForecastSnapshot], DayClassification],
    base_floor: Callable[[datetime, DayClassification, bool], float],
    max_offset_f: float,
) -> dict[str, Any]:
    """The ``preference_seed`` for a recorder window (see the spec's steps 2 to 6).

    ``classify`` turns a day's outdoor high/low into its classification (the coordinator's ``classify_day`` thresholds)
    and ``base_floor(local_at, classification, asleep)`` is today's configured heat floor without any learned offset.
    Occupancy is not known historically and is assumed home. ``ref`` is null (the history was steered by the
    thermostat). The thermostat's own reading is kept under ``thermostat`` with the room temperatures.
    """
    wake, sleep = config.get("wake_time"), config.get("sleep_time")
    rows = sorted(climate, key=lambda r: r.at)
    first = rows[0].at if rows else end
    days_covered = max(1, round((end - first) / timedelta(days=1)))  # round: a DST day is 23 or 25 hours
    changes = setpoint_changes(rows)
    mask = schedule_mask(changes, tz, days_covered)
    kept = [c for c, sched in zip(changes, mask, strict=True) if not sched]

    temps: dict[str, Series] = dict(rooms)
    temps[REF_THERMOSTAT] = [(r.at, r.current_f) for r in rows if r.current_f is not None]
    temp_lookups = {entity: _Lookup(series) for entity, series in temps.items()}
    humidity_lookup = _Lookup(humidity)
    bands = _band_series(sorted(humidity, key=lambda p: p[0]))
    band_times = [t for t, _ in bands]
    daily = _daily_outdoor(outdoor, tz)
    classifications: dict[Any, DayClassification] = {}

    def classification_for(day: Any) -> DayClassification | None:
        if day not in classifications and day in daily:
            high, low = daily[day]
            tomorrow = daily.get(day + timedelta(days=1), daily[day])
            classifications[day] = classify(
                ForecastSnapshot(
                    today_high=high,
                    today_low=low,
                    tomorrow_high=tomorrow[0],
                    tomorrow_low=tomorrow[1],
                    current_outdoor_temp=high,
                )
            )
        return classifications.get(day)

    observations: list[dict] = []
    no_outdoor = 0
    episode: dict | None = None
    for change in kept:
        loc = change.at.astimezone(tz)
        asleep = in_sleep_window(loc, sleep, wake)
        c = classification_for(loc.date())
        if c is None:
            no_outdoor += 1
            continue
        i = bisect_right(band_times, change.at)
        band = bands[i - 1][1] if i else BAND_NONE
        rh = humidity_lookup.at(change.at)
        room_temps = {
            e: round(v, 2) for e, lk in temp_lookups.items() if (v := lk.at(change.at, ROOM_MAX_AGE)) is not None
        }
        base = base_floor(loc, c, asleep)
        context: dict[str, Any] = {"ref": None, "band": band, "rooms": room_temps}
        if rh is not None:
            context["rh"] = round(rh, 1)
        # target_f = old: the CA target of the time is unknown, so an undo is a return to where the occupant started.
        episode, closed = extend_episode(
            episode,
            at=loc,
            old_f=change.old_f,
            new_f=change.new_f,
            bucket=bucket_key(loc, str(c.day_type), wake, sleep, in_sleep_window=asleep, band=band),
            base_f=base,
            target_f=change.old_f,
            excluded=near_schedule_edge(loc, wake, sleep),
            context=context,
        )
        if closed is not None:
            observations.append({**closed, "source": SOURCE_HISTORY})
    if episode is not None and (last := close_episode(episode)) is not None:
        observations.append({**last, "source": SOURCE_HISTORY})

    return {
        "created": end.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days_covered},
        "counts": {
            "changes": len(changes),
            "schedule": len(changes) - len(kept),
            "no_outdoor": no_outdoor,
            "observations": len(observations),
        },
        "observations": observations,
        "summary": seed_offsets(observations, max_offset_f=max_offset_f),
        "background": background_samples(temps, start=first, end=end, tz=tz, config=config),
    }
