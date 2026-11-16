<!-- Nav: ← [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) -->

# Preference Learning (fork feature, Dunky13#1)

Status: phase 1 specified, phases 2 and 3 planned. Fork-only feature; upstream is gunkl/ClimateAdvisor.

## Goal

Climate Advisor learns the comfort temperature the household actually wants from the occupant's own thermostat
changes, separately per weather, part of the day and weekday/weekend, and applies it automatically within safe
bounds. This implements the "Override Pattern Analysis" future-work item in
[05-LEARNING-ENGINE-DESIGN.md](05-LEARNING-ENGINE-DESIGN.md#override-pattern-analysis) and follows the project rule
"repeated overrides in same context = learned preference; single overrides = note but don't learn yet" (claude.md).

## Accepted decisions (owner, 2026-11-16)

| # | Decision |
|---|----------|
| D1 | Learned preferences are applied automatically, bounded: at most ±2 °C from the configured setpoint, moving at most 0.5 °C per week. Visible on the dashboard, with a reset. |
| D2 | Preference dimensions: weather day type × part of day × weekday/weekend, including learned weekday and weekend wake and bed times. |
| D3 | The user can select one or more indoor room temperature sensors in setup/options; CA learns which room matters in which part of the day (for example bedroom at night and in the morning, living room in the afternoon). |
| D4 | Delivered in phases: phase 1 = setpoint preference learning (D1, D2 without wake/bed times). Phase 2 = learned weekday/weekend wake and bed times. Phase 3 = multi-room sensors (D3). Each phase is its own PR, stacked when the base is unmerged. |
| D5 | Heating only. The target install is a heat-only thermostat (Google Nest, heat/off). Cooling preferences are out of scope; the cooling ceiling is never changed. |

## Phase 1: setpoint preference learning

### Off by default

The feature is disabled until the user enables it. Disabled means: no observation is recorded, every learned offset
reads as 0, no output (plan, chart, briefing, write, status) differs from upstream, and all golden simulations stay
unchanged (`tools/simulate.py --check-integrity`).

### Settings (options flow, new step `preference_learning`)

| Key | Type | Default | Range | Notes |
|-----|------|---------|-------|-------|
| `preference_learning_enabled` | bool | `False` | | |
| `preference_max_offset_f` | °F delta | `3.6` (2 °C) | 0 to 5.4 (3 °C) | Shown in the user's unit via the delta-field pattern (`_DELTA_FIELDS_F`). |
| `preference_weekly_step_f` | °F delta | `0.9` (0.5 °C) | 0.18 to 1.8 | Maximum change of one bucket's offset per weekly update. |

Follow the Config UI Field Standard in claude.md: `CONFIG_METADATA` defaults, `strings.json` = `translations/en.json`,
all 16 languages via `tools/i18n.py`, menu list test. Code reads these with `config.get(KEY, DEFAULT)`; no entry
migration.

Accepting the existing `frequent_overrides` suggestion enables preference learning (replaces the
`request_setpoint_analysis` placeholder in `LearningEngine.accept_suggestion`; update the tests that pin it).

### Buckets

A bucket key is `"<day_type>|<part_of_day>|<day_class>"`, for example `cold|evening|weekday`.

- **day_type**: CA's current classification at the moment (`hot`, `warm`, `mild`, `cool`, `cold`). Use the live
  classification, not `DailyRecord.day_type` (which is frozen at the day's first classification).
- **part_of_day**: `night` when inside the sleep window (`_in_sleep_window`); otherwise by local clock: `morning`
  from wake time to 12:00, `afternoon` 12:00 to 17:00, `evening` 17:00 to bedtime. If the wake time is at or after
  12:00 the morning part is empty; the same clipping applies to the other edges.
- **day_class**: `weekend` for Saturday and Sunday, otherwise `weekday`. A night belongs to the day it wakes into
  (precedent: `pre_cool_waking_date`), so Friday night counts as `weekend` and Sunday night as `weekday`.

A pure function computes the bucket from (local datetime, day type, wake time, sleep time); it is the single source
for every caller.

### Observations

An observation is recorded only when all of these hold at the time of the occupant's setpoint change:

1. Preference learning is enabled.
2. Automation is enabled (CA is in control). In observe-only mode every thermostat change, including the
   thermostat's own schedule, counts as the occupant's, so nothing is learned.
3. The thermostat's mode is `heat` and the change is an occupant override as already detected by
   `_async_thermostat_changed` (CA's own writes and echoes are already excluded there).
4. Neither the old nor the new state has `preset_mode` `eco` (Nest Eco changes the setpoint without the occupant).
5. Occupancy is home or guest (not away or vacation): the learned value is a comfort preference, not a setback.
6. The episode does not start within 60 minutes before the wake time or within 60 minutes before bedtime (midnight
   wrap handled). Pre-wake heat-ups and pre-bed lowerings are schedule-timing signals, not temperature preferences;
   without this exclusion they drive the night offset to the cap and erode the overnight setback. They are not learned
   from in phase 1 (see Phase 2). Changes that extend an episode which started outside the window still count.

Observations are grouped into an **episode**: further occupant changes within 15 minutes of the previous one extend
the episode, and the episode's final setpoint is the one that counts. When the episode closes, its observation is:

`delta_f = final_setpoint_f − base_target_f`

where `base_target_f` is CA's heat floor for that moment **without** any learned offset. Using the base (not the
offset-adjusted target) makes the offset converge to the preference instead of accumulating. An episode whose final
setpoint is within 0.5 °F of the offset-adjusted target the occupant started from is discarded (the occupant undid
their change).

The thermostat reports the setpoint it holds, which includes any applied bedroom remote-sensor offset. Both reported
setpoints are brought back to the comfort-target frame (the applied signed offset is subtracted) before the delta and
the undo check, so a remote offset is never learned as a preference. The offset is captured when the episode opens and
stored in it (`remote_f`, 0 for an episode saved without it); every later change in the episode uses that value, because
the remote offset stops being applied once the occupant takes the thermostat.

Each stored observation: `{"date": "YYYY-MM-DD", "time": "HH:MM", "bucket": key, "delta_f": float}`.

Retention: 400 days, capped at 2000 entries (oldest dropped first). This is deliberately longer than the 90-day
`DailyRecord` retention so last winter's cold-day observations are still there next winter.

### Weekly update

Runs from the end-of-day processing once at least 7 days have passed since the previous update (stored as a date).
For each bucket:

1. Take the bucket's most recent 20 observations, at any age within retention.
2. If fewer than 3, fall back to the bucket's `day_type|part_of_day` across both day classes; if still fewer than 3,
   no change for this bucket.
3. `target = median(delta_f)` of the level used, clamped to ±`preference_max_offset_f`.
4. `new = current + clamp(target − current, ±preference_weekly_step_f)`, then clamped to
   ±`preference_max_offset_f`, rounded to 0.1 °F.
5. Log every change at INFO (bucket, level used, n, target, old, new) and append it to a history list (cap 200).

Lowering `preference_max_offset_f` takes effect immediately on application (offsets are clamped when read).

### Application

The learned offset for the current bucket is added to the **heat floor** in exactly two branches of the comfort-band
resolver: awake with occupancy home/guest, and the sleep window. Never to away, vacation or setback floors, and never
to the cooling ceiling. The result stays within the thermostat's configurable range, and the offset never leaves the
heat floor closer than `MIN_HEAT_COOL_GAP_F` (4 F) to the ceiling (a band already narrower keeps its configured floor).

There must be one seam: `select_comfort_band` / `awake_comfort_edges` (automation.py) receive the moment (`at`) and
the offset lookup, and every consumer reads the band through them, so the plan, chart band, briefing,
next-automation text, timeline and the actual `climate.set_temperature` write all agree (the "one band" rule,
#1080/#1087). The known bypass readers that must be rerouted through the seam:

- `coordinator._compute_target_band_schedule` (chart band)
- the bedtime next-automation text that reads `CONF_SLEEP_HEAT` directly
- `automation.compute_bedtime_setback`
- `automation.fallback_edges_without_classification`
- briefing paths that read raw `comfort_heat` / `sleep_heat`

Grep every raw read of `comfort_heat`, `sleep_heat` and `setback_heat` across the package and classify each one as
"comfort target, must go through the seam" or "not a target (validation, display of the configured value, etc.)".
Do not apply the offset in `_set_temperature_impl`.

A bucket change (for example afternoon to evening) takes effect at the next band application (the 30-minute refresh
or the wake/bedtime handlers). No new timers in phase 1.

The bedroom remote offset (`remote_offset.py`) is applied after the band and stacks with the learned offset; this is
intended.

### Persistence

New `LearningState` fields: `preference_offsets` (`{bucket: {"offset_f": float, "n": int, "level": str,
"updated": "YYYY-MM-DD"}}`), `preference_observations` (list), `preference_history` (list),
`preference_last_update` (date string or null), and the open episode if one must survive a restart. Validate types in
`load_state` like the existing fields. `reset_learning_data` gains a `preferences` scope that clears these fields only.
Note: an upstream build would drop these unknown keys on load; document that downgrading to upstream discards the
learned preferences.

### Visibility

- Status / diagnostics: enabled flag, current bucket, active offset, observation count, last update date. The Status
  tab shows one line (inside the existing thermostat card) with the armed heat floor against the configured one and
  why they differ.
- Dashboard (Analysis tab): a table of buckets with the applied (cap-clamped) offset, n and last update (the stored
  value beside it when the maximum was lowered below it), and a confirmed reset that clears only the preferences
  scope (owner decision D1: visible on the dashboard, with a reset).
- Briefing: one line naming every non-zero offset still ahead today and tonight for the current day type and day
  class, for example "Learned preference (Cold, weekday): evening +0.5 °C, tonight -0.5 °C", in the user's unit and
  through i18n.
- Follow the repo's mandatory logging, status-page field and chart coverage rules (claude.md).

### Tests

- Pure-module tests: bucket keys including clipping and the Friday/Sunday night rule; episode grouping and the undo
  discard; weekly update with step and max clamps, the fallback level, the minimum of 3, median, rounding.
- Engine tests: the offset reaches the heat floor in awake-home and sleep, never away/vacation/setback or cooling; the
  disabled feature changes nothing.
- Coordinator tests: observation gating (automation off, eco preset, away, non-heat mode, CA's own writes).
- Consistency test: with an offset active, the chart band, briefing target and written setpoint agree.
- Config flow / metadata / strings tests updated; golden simulations unchanged.

### Implementation notes (phase 1, as built)

- Pure logic lives in `preference_learning.py` (no Home Assistant imports); `LearningEngine` owns the state and `coordinator.preference_view()` hands the band resolver a read-only `PreferenceView`, or `None` while the feature is off (every offset 0, output identical to upstream).
- Observations also skip while a time-of-use bank holds the setpoint (the held target is not the occupant's comfort preference).
- Raw-read classification. Rerouted through the seam (comfort targets): `select_comfort_band` call sites, `awake_comfort_edges`, `compute_bedtime_setback`, `_compute_target_band_schedule`, the briefing's awake and bedtime figures, the bedtime next-automation text, the chart's predicted-indoor band. Untouched on purpose (not comfort targets): cooling and natural-ventilation guards and fan inputs, `fallback_edges_without_classification` (no day type, so no bucket; offset 0), the away/vacation/setback reads, config-flow validation and `setpoint_combinations.py`, the display of configured values. The invariant watchdog's heat-floor promises (P1, P7) shift the configured floor by the signed offset the band resolver actually applies for the current bucket (same view), so they judge against the real armed floor and stay as strict as before when every offset is 0. The Comfort Score reads the resolved band, which already carries the offset.
- Natural-ventilation and fan-cycling floors, the welcome-home comfort check, the morning pre-cool guard, the AI comfort-band text and the nat-vent plan read the learned heat floor (`automation.learned_heat_floors`), so ventilation never cools below the armed floor. The nat-vent plan uses one scalar for the whole plan (the offset as of now). `setback_depth_f` in the day record stays comfort minus the armed sleep floor.
- The `respond_to_suggestion` service and the dashboard accept share one apply-and-persist path (`api.apply_accepted_changes`).
- Downgrading to upstream discards the learned preferences (upstream drops the unknown state keys on load).

### Documentation and release

- This spec; update [05-LEARNING-ENGINE-DESIGN.md](05-LEARNING-ENGINE-DESIGN.md) to point here; README section with
  the thermostat requirements below; `CHANGELOG.md` entry; fork version `0.9.41.1` in `const.py` and
  `manifest.json`.
- Fork versioning: fork releases carry a fourth component, `0.9.41.N`, so they never collide with an upstream
  release (`0.9.41`, `0.9.42`, ...). The fork's own `fix_history.jsonl` entries are not recorded for fork issues:
  `tools/add_fix_entry.py` resolves an issue number against the upstream repository (`GITHUB_REPO`), so the fork
  issue (Dunky13/ClimateAdvisor#1) would be read as upstream issue 1; the CHANGELOG is the fork's record.

### Thermostat requirements (README)

Learning is only meaningful when Climate Advisor is the only thing that sets the temperature apart from the
occupants. On a Google Nest, turn off the Nest schedule, Eco temperatures, Home/Away Assist and Auto-Schedule in the
Google Home app before enabling automation and preference learning.

## Phase 2: learned wake and bed times (planned)

Separate weekday and weekend wake and bed times, learned from when the occupant's morning raises and evening
lowers happen, bounded by a maximum shift per week. Requires a date-aware sleep window, re-registered wake/bed
timers, and rerouting the ~15 direct parsers of `wake_time` / `sleep_time`. Specified before implementation. The
pre-wake and pre-bed episodes that phase 1 excludes from setpoint learning (the 60-minute schedule-edge window) are
the input for these learned wake and bed times.

## Phase 3: room sensors (planned)

The user selects room temperature sensors (multi-select, options and setup). CA records every room's temperature at
each observation and learns per part of day which room the occupants react to, generalising the existing bedroom
sensor swap in `indoor_temp.resolve_indoor_temp_with_provenance` and the remote offset. Specified before
implementation.
