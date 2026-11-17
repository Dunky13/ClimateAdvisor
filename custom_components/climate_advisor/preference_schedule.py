"""Learned wake and bed times: the pure core (fork Dunky13#1, phase 2).

Occupant outcome: when the household gets up and goes to bed at other times than the configured ones (later on days
off, later on a holiday), Climate Advisor learns the shift per weekday/weekend and moves its wake and bed handling
with it, within safe bounds.

This module is pure (no Home Assistant, no clock): the day class (with the optional Workday mapping), the effective
schedule (the one seam every reader of "the schedule at this moment" goes through), candidate extraction from signal
transitions and the weekly update. The coordinator owns the state, the signal listeners and the timers.

Spec: docs/preference-learning-spec.md, "Phase 2: learned wake and bed times".
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from statistics import median
from typing import Any, NamedTuple

from .const import DEFAULT_SCHEDULE_MAX_SHIFT_MIN

DAY_CLASS_WEEKDAY = "weekday"
DAY_CLASS_WEEKEND = "weekend"
DAY_CLASSES: tuple[str, ...] = (DAY_CLASS_WEEKDAY, DAY_CLASS_WEEKEND)
KIND_WAKE = "wake"
KIND_BED = "bed"
KINDS: tuple[str, ...] = (KIND_WAKE, KIND_BED)
SOURCE_SENSOR = "sensor"
SOURCE_THERMOSTAT = "thermostat"
SOURCE_HISTORY = "history"
CANDIDATE_SOURCES: tuple[str, ...] = (SOURCE_SENSOR, SOURCE_THERMOSTAT, SOURCE_HISTORY)

SIGNAL_WINDOW_MIN = 180  # a candidate lies within 3 hours of the configured time
FILTER_TOLERANCE_MIN = 2  # the same clock time: automations fire to the second
FILTER_MIN_SHARE = 0.4  # of the days the history covers
FILTER_MIN_DAYS = 4
MIN_CANDIDATES = 4
RECENT_CANDIDATES = 20
ROUND_MIN = 5
RETENTION_DAYS = 400
MAX_CANDIDATES = 2000
_DAY_MIN = 24 * 60
_NOON_HOUR = 12
EVENT_LIVE_WINDOW = timedelta(seconds=60)  # an event this close to now is live, not the restored last event
_UNKNOWN_STATES = frozenset({"unavailable", "unknown"})


def clock_minutes(value: Any) -> int | None:
    """``"HH:MM"`` / ``"HH:MM:SS"`` as minutes after midnight, else None (unset or malformed)."""
    try:
        parts = str(value).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError, IndexError, TypeError:
        return None


def hhmm(value: Any) -> str | None:
    """A configured or shifted time as ``"HH:MM"``, None when unset or malformed."""
    minutes = clock_minutes(value)
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


def day_class(day: date, workdays: Mapping[date, bool] | None = None) -> str:
    """``weekday`` for a workday, ``weekend`` for a day off.

    ``workdays`` is the ``{date: is_workday}`` mapping the coordinator resolved from the Workday sensor (holidays are
    days off); a date missing from it (or no mapping) follows the weekday rule: Saturday and Sunday are days off.
    """
    is_workday = workdays.get(day) if workdays else None
    if is_workday is None:
        is_workday = day.weekday() < 5
    return DAY_CLASS_WEEKDAY if is_workday else DAY_CLASS_WEEKEND


def clamp_minutes(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def round_minutes(value: float) -> int:
    """Round half up to a multiple of 5 minutes (``round`` would send 2.5 to 2)."""
    return int(math.floor(value / ROUND_MIN + 0.5)) * ROUND_MIN


def wrap_minutes(value: float) -> int:
    """A signed difference of minutes wrapped to ``[-720, 720)``: a bedtime past midnight is a small positive shift."""
    return int((round(value) + _DAY_MIN // 2) % _DAY_MIN - _DAY_MIN // 2)


def shift_time(configured: Any, shift_min: int) -> Any:
    """The configured time plus the shift, as ``"HH:MM"`` wrapping past midnight.

    The configured value is returned verbatim (``"06:30:00"``, unset, malformed) when there is nothing to shift, so a
    schedule without a learned shift is byte-identical to the configured one.
    """
    minutes = clock_minutes(configured)
    if not shift_min or minutes is None:
        return configured
    shifted = (minutes + shift_min) % _DAY_MIN
    return f"{shifted // 60:02d}:{shifted % 60:02d}"


class WakeBed(NamedTuple):
    """The wake and bed times of one night, always in this order (``"HH:MM"`` strings, or ``time`` for a timeline)."""

    wake: Any
    bed: Any


@dataclass(frozen=True)
class ScheduleView:
    """The learned shifts as the readers see them: a read-only snapshot (it is read from executor threads).

    ``shifts`` is ``{"weekday": {"wake": min, "bed": min}, "weekend": {...}}`` as stored; ``max_shift_min`` the current
    cap (lowering it applies at once, shifts are clamped when read); ``workdays`` the resolved ``{date: is_workday}``.
    """

    shifts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    max_shift_min: int = DEFAULT_SCHEDULE_MAX_SHIFT_MIN
    workdays: Mapping[date, bool] = field(default_factory=dict)

    def shift(self, cls: str, kind: str) -> int:
        """The shift (minutes) for a class and kind, clamped to the current cap."""
        raw = self.shifts.get(cls, {}).get(kind, 0)
        return int(clamp_minutes(raw, self.max_shift_min)) if is_minutes(raw) else 0

    @property
    def active(self) -> bool:
        """True when any shift applies (a cap of 0 or all-zero shifts leave the configured times alone)."""
        return self.max_shift_min > 0 and any(self.shift(c, k) for c in DAY_CLASSES for k in KINDS)

    def day_class(self, day: date) -> str:
        return day_class(day, self.workdays)

    def effective_times(self, config: Mapping[str, Any], day: date) -> tuple[Any, Any]:
        """``(wake, bed)``: the wake time on the morning of ``day`` and the bedtime on the evening of ``day``.

        The wake uses the class of ``day``, the bedtime the class of ``day + 1`` (the day that night wakes into).
        Each is the configured time plus the clamped shift, as ``"HH:MM"`` (verbatim when unshifted).
        """
        wake = shift_time(config.get("wake_time"), self.shift(self.day_class(day), KIND_WAKE))
        bed = shift_time(config.get("sleep_time"), self.shift(self.day_class(day + timedelta(days=1)), KIND_BED))
        return wake, bed

    def schedule_at(self, config: Mapping[str, Any], at: datetime) -> WakeBed:
        """The ``WakeBed`` of the night that matters at ``at`` (the noon pivot).

        Before 12:00 that is the night that ends this morning (yesterday's bedtime, today's wake); from 12:00 the coming
        night (today's bedtime, tomorrow's wake).
        """
        today = at.date()
        if at.hour < _NOON_HOUR:
            return WakeBed(
                self.effective_times(config, today)[0], self.effective_times(config, today - timedelta(days=1))[1]
            )
        return WakeBed(
            self.effective_times(config, today + timedelta(days=1))[0], self.effective_times(config, today)[1]
        )


def is_minutes(value: Any) -> bool:
    """A whole number of minutes (bool is not one)."""
    return isinstance(value, int) and not isinstance(value, bool)


def config_at(view: ScheduleView | None, config: dict, at: datetime) -> dict:
    """``config`` with its ``sleep_time`` and ``wake_time`` replaced by the schedule at ``at``.

    Returns ``config`` itself when nothing is learned (no view, a cap of 0, all shifts 0): the off path is identical.
    """
    if view is None or not view.active:
        return config
    night = view.schedule_at(config, at)
    return {**config, "sleep_time": night.bed, "wake_time": night.wake}


# --------------------------------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------------------------------


def candidate_for(
    kind: str, at: datetime, config: Mapping[str, Any], workdays: Mapping[date, bool] | None = None
) -> dict[str, Any] | None:
    """The candidate a signal at local ``at`` makes, or None when it lies outside the 3-hour window.

    ``{"date", "class", "kind", "delta_min"}``: ``date`` is the waking date, ``delta_min`` the signed wall-clock
    distance from the configured time (within 3 hours, so it needs no wrapping). A bedtime after midnight belongs to the
    previous evening, and a configured bedtime after midnight (earlier than the wake time on the clock) is that
    evening's after-midnight instant. The wall clock is used on purpose: the distance from "23:00" is the same on a
    DST night.
    """
    wake_min = clock_minutes(config.get("wake_time"))
    bed_min = clock_minutes(config.get("sleep_time"))
    configured = wake_min if kind == KIND_WAKE else bed_min
    if configured is None:
        return None
    naive = at.replace(tzinfo=None)
    best: tuple[int, date] | None = None
    for offset in (-1, 0, 1):
        anchor = at.date() + timedelta(days=offset)  # a morning (wake) or an evening (bed)
        target_day = anchor
        if kind == KIND_BED and wake_min is not None and bed_min is not None and bed_min < wake_min:
            target_day = anchor + timedelta(days=1)  # the bedtime itself is after midnight
        target = datetime.combine(target_day, time(configured // 60, configured % 60))
        delta = round((naive - target).total_seconds() / 60)
        if abs(delta) <= SIGNAL_WINDOW_MIN and (best is None or abs(delta) < abs(best[0])):
            best = (delta, anchor if kind == KIND_WAKE else anchor + timedelta(days=1))
    if best is None:
        return None
    delta, waking = best
    return {"date": waking.isoformat(), "class": day_class(waking, workdays), "kind": kind, "delta_min": delta}


def merge_candidate(candidates: list[dict], new: dict[str, Any], *, replace: bool) -> bool:
    """Add ``new`` to ``candidates`` (one per waking date and kind); True when the list changed.

    ``replace`` True keeps the later signal (a bedtime is the last deactivation of the night); False keeps the first
    (a wake is the first activation of the morning, and the thermostat fallback is the first episode).
    """
    for i, existing in enumerate(candidates):
        if existing["date"] == new["date"] and existing["kind"] == new["kind"]:
            if not replace or existing == new:
                return False
            candidates[i] = new
            return True
    candidates.append(new)
    return True


def trim_candidates(candidates: list[dict], today: date) -> list[dict]:
    """Keep 400 days and at most 2000 entries (oldest dropped first), ordered by waking date."""
    cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
    kept = sorted((c for c in candidates if str(c.get("date", "")) >= cutoff), key=lambda c: str(c["date"]))
    return kept[-MAX_CANDIDATES:]


def transition_active(domain: str, old: Any, new: Any, kind: str, now: datetime | None = None) -> bool:
    """True for a real qualifying transition of a signal entity of ``domain``.

    Both states must be known (not ``unavailable`` / ``unknown`` / missing): a restart or a dropped device never counts.
    A wake is an activation (``on``), a bed a deactivation (``off``). An ``event`` entity's state is the time of its
    last event, so any new event counts; when the old state is unknown (a restart), the event still counts if its
    timestamp is within a minute of ``now``, which the restore of the last event at startup never is.
    """
    if new is None or new in _UNKNOWN_STATES or old == new:
        return False
    if domain == "event":
        if old is not None and old not in _UNKNOWN_STATES:
            return True
        try:
            fired = datetime.fromisoformat(str(new))
        except ValueError:
            return False
        return now is not None and fired.tzinfo is not None and abs(now - fired) <= EVENT_LIVE_WINDOW
    if old is None or old in _UNKNOWN_STATES:
        return False
    return new == ("on" if kind == KIND_WAKE else "off")


def row_transitions(domain: str, rows: Iterable[tuple[datetime, str]], kind: str) -> list[datetime]:
    """The qualifying transition instants in a history read: ``[(at, state), ...]`` oldest first.

    The first row is the synthetic start-of-window state (its old state is unknown), so it is never a transition.
    """
    out: list[datetime] = []
    previous: str | None = None
    for at, state in sorted(rows, key=lambda r: r[0]):
        if transition_active(domain, previous, state, kind):
            out.append(at)
        previous = state
    return out


def schedule_filter(transitions: list[datetime], days_covered: int) -> list[bool]:
    """True for each transition that is an automation's: the same clock time (+-2 minutes) on enough days.

    A transition is scheduled when other transitions of the same entity and direction fall within +-2 minutes of the
    same local clock time on at least 40 % of the days the history covers and on at least 4 days (the phase 3 rule
    with the tolerance for a signal: automations fire to the second, so +-10 minutes would also remove a person who
    switches the light off at 23:54 near an automation's 23:59). ``transitions`` are local instants.
    """
    need = max(FILTER_MIN_DAYS, math.ceil(FILTER_MIN_SHARE * days_covered))
    out: list[bool] = []
    for at in transitions:
        near = {other.date() for other in transitions if _second_gap(at, other) <= FILTER_TOLERANCE_MIN * 60}
        out.append(len(near) >= need)
    return out


def _second_gap(a: datetime, b: datetime) -> float:
    """Seconds between the clock times of two instants, wrapping midnight."""
    da = a.hour * 3600 + a.minute * 60 + a.second
    db = b.hour * 3600 + b.minute * 60 + b.second
    d = abs(da - db)
    return min(d, _DAY_MIN * 60 - d)


def history_candidates(
    signals: Mapping[str, Mapping[str, list[datetime]]],
    *,
    config: Mapping[str, Any],
    workdays: Mapping[date, bool] | None,
    days_covered: int,
) -> tuple[list[dict], dict[str, int]]:
    """Candidates (source ``history``) from the qualifying transitions of every signal entity: ``(candidates, counts)``.

    ``signals`` is ``{kind: {entity: [local transition instants]}}``. Each entity's transitions pass the +-2-minute
    schedule filter on their own, then every kept transition of a kind is turned into a candidate; per waking date the
    first wake and the last bed are kept. ``counts``: ``transitions``, ``schedule`` (removed) and ``outside``
    (not within     3 hours of the configured time). Occupancy is not known historically; it is assumed home.
    """
    out: list[dict] = []
    counts = {"transitions": 0, "schedule": 0, "outside": 0}
    for kind in KINDS:
        found: dict[str, dict] = {}
        for times in signals.get(kind, {}).values():
            ordered = sorted(times)
            mask = schedule_filter(ordered, days_covered)
            counts["transitions"] += len(ordered)
            counts["schedule"] += sum(mask)
            for at, scheduled in zip(ordered, mask, strict=True):
                if scheduled:
                    continue
                cand = candidate_for(kind, at, config, workdays)
                if cand is None:
                    counts["outside"] += 1
                    continue
                known = found.get(cand["date"])
                better = known is None or (
                    cand["delta_min"] < known["delta_min"]
                    if kind == KIND_WAKE
                    else cand["delta_min"] > known["delta_min"]
                )
                if better:
                    found[cand["date"]] = cand
        out.extend({**c, "source": SOURCE_HISTORY} for c in found.values())
    return sorted(out, key=lambda c: (c["date"], c["kind"])), counts


# --------------------------------------------------------------------------------------------------
# Weekly update and the seed review rows
# --------------------------------------------------------------------------------------------------


def recent_deltas(candidates: Iterable[Mapping[str, Any]], cls: str, kind: str) -> list[int]:
    """The deltas of the most recent 20 candidates of a class and kind (ordered by waking date)."""
    chosen = sorted((c for c in candidates if c["class"] == cls and c["kind"] == kind), key=lambda c: str(c["date"]))
    return [wrap_minutes(c["delta_min"]) for c in chosen[-RECENT_CANDIDATES:]]


def target_shift(deltas: list[int], max_shift_min: int) -> int:
    """The median of the (wrapped) deltas, clamped to the cap and rounded to 5 minutes."""
    return round_minutes(clamp_minutes(float(median(deltas)), max_shift_min))


def weekly_schedule_update(
    shifts: Mapping[str, Mapping[str, Any]],
    candidates: Iterable[Mapping[str, Any]],
    *,
    max_shift_min: int,
    step_min: int,
    today: date,
) -> tuple[dict[str, dict[str, int]], list[dict]]:
    """One weekly pass over the four class and kind pairs: ``(new_shifts, changes)``.

    Per pair, the most recent 20 candidates; fewer than 4 means no change. ``target`` is their median clamped to
    ``+-max_shift_min``; ``new = current + clamp(target - current, +-step_min)`` from the stored shift clamped to the
    cap, clamped again and rounded to 5 minutes.
    ``changes`` has one record per pair whose shift moved.
    """
    pool = list(candidates)
    new_shifts = {c: {k: int(shifts.get(c, {}).get(k, 0)) for k in KINDS} for c in DAY_CLASSES}
    changes: list[dict] = []
    for cls in DAY_CLASSES:
        for kind in KINDS:
            deltas = recent_deltas(pool, cls, kind)
            if len(deltas) < MIN_CANDIDATES:
                continue
            stored = new_shifts[cls][kind]
            current = int(clamp_minutes(stored, max_shift_min))  # shifts are clamped when read: step from what applies
            target = clamp_minutes(float(median(deltas)), max_shift_min)
            new = round_minutes(clamp_minutes(current + clamp_minutes(target - current, step_min), max_shift_min))
            new_shifts[cls][kind] = new
            if new != stored:
                changes.append(
                    {
                        "date": today.isoformat(),
                        "kind": "schedule",
                        "class": cls,
                        "signal": kind,
                        "n": len(deltas),
                        "target_min": round_minutes(target),
                        "old_min": current,
                        "new_min": new,
                    }
                )
    return new_shifts, changes


def seed_schedule_rows(
    candidates: Iterable[Mapping[str, Any]], shifts: Mapping[str, Mapping[str, Any]], *, max_shift_min: int
) -> dict[str, dict[str, Any]]:
    """``{"<class>|<kind>": {"n", "median_min", "current_min", "new_min", "enabled"}}`` for the review panel.

    ``new_min`` is the median clamped to the cap and rounded (the weekly step does not limit an accepted seed); a row
    with fewer than 4 candidates is ``enabled`` False (shown disabled, and Accept never applies it).
    """
    pool = list(candidates)
    rows: dict[str, dict[str, Any]] = {}
    for cls in DAY_CLASSES:
        for kind in KINDS:
            deltas = recent_deltas(pool, cls, kind)
            if not deltas:
                continue
            enabled = len(deltas) >= MIN_CANDIDATES
            rows[schedule_row_key(cls, kind)] = {
                "n": len(deltas),
                "median_min": round_minutes(float(median(deltas))),
                "current_min": int(shifts.get(cls, {}).get(kind, 0)),
                "new_min": target_shift(deltas, max_shift_min) if enabled else None,
                "enabled": enabled,
            }
    return rows


def schedule_row_key(cls: str, kind: str) -> str:
    return f"{cls}|{kind}"


def parse_schedule_row_key(key: Any) -> tuple[str, str] | None:
    """``(class, kind)`` of a well-formed seed row key, else None (never raises)."""
    if not isinstance(key, str):
        return None
    parts = key.split("|")
    if len(parts) == 2 and parts[0] in DAY_CLASSES and parts[1] in KINDS:
        return parts[0], parts[1]
    return None


# --------------------------------------------------------------------------------------------------
# The effective schedule for the readers that look ahead
# --------------------------------------------------------------------------------------------------


def instants_on(
    view: ScheduleView | None, config: Mapping[str, Any], day: date, tz: tzinfo | None
) -> dict[str, datetime | None]:
    """The wake of the morning of ``day`` and the bedtime of the evening of ``day`` as instants (None when unparseable).

    A bedtime after midnight (earlier than the next wake on the clock) lands on the next calendar day.
    """
    schedule = view if view is not None else ScheduleView()
    wake, bed = schedule.effective_times(config, day)
    out: dict[str, datetime | None] = {KIND_WAKE: None, KIND_BED: None}
    for kind, value in ((KIND_WAKE, wake), (KIND_BED, bed)):
        minutes = clock_minutes(value)
        if minutes is None:
            continue
        at = datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=tz)
        if kind == KIND_BED:
            wake_min = clock_minutes(schedule.effective_times(config, day + timedelta(days=1))[0])
            if wake_min is not None and minutes < wake_min:
                at += timedelta(days=1)
        out[kind] = at
    return out


def night_times_fn(
    view: ScheduleView | None, config: Mapping[str, Any]
) -> Callable[[date], tuple[tuple[int, int], tuple[int, int]]] | None:
    """``evening date -> ((h, m) bed, (h, m) wake)`` of the learned night, None while nothing is learned.

    For the nights the replay validates (``night_replay.find_nights``); an unparseable time falls back to 22:30 / 06:30.
    """
    if view is None or not view.active:
        return None

    def _hm(value: Any, default: int) -> tuple[int, int]:
        minutes = clock_minutes(value)
        return divmod(default if minutes is None else minutes, 60)

    def _night(day: date) -> tuple[tuple[int, int], tuple[int, int]]:
        bed = view.effective_times(config, day)[1]
        wake = view.effective_times(config, day + timedelta(days=1))[0]
        return _hm(bed, 22 * 60 + 30), _hm(wake, 6 * 60 + 30)

    return _night


def next_times(view: ScheduleView | None, config: Mapping[str, Any], after: datetime) -> dict[str, datetime | None]:
    """The next effective wake and bedtime strictly after ``after`` (local, aware): ``{"wake": dt, "bed": dt}``.

    Candidates are the days around ``after`` (yesterday's through the day after tomorrow's evening, since a bedtime past
    midnight lands on the next calendar day). A view with nothing learned still answers from the configured times.
    """
    best: dict[str, list[datetime]] = {KIND_WAKE: [], KIND_BED: []}
    for offset in range(-1, 3):
        found = instants_on(view, config, after.date() + timedelta(days=offset), after.tzinfo)
        for kind, at in found.items():
            if at is not None and at > after:
                best[kind].append(at)
    return {kind: min(found) if found else None for kind, found in best.items()}
