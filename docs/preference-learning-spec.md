<!-- Nav: ← [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) -->

# Preference Learning (fork feature, Dunky13#1)

Status: phase 1 shipped (0.9.41.1); phase 3 (rooms, humidity, history backfill) shipped (0.9.41.2); phase 2 (learned
wake and bed times) shipped (0.9.41.3). Fork-only feature; upstream is gunkl/ClimateAdvisor.

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
| D6 | (2026-11-16) Rooms steer the thermostat: CA learns which room the occupants react to per part of the day and nudges the one thermostat setpoint so that room reaches the learned comfort temperature. Bounded like D1. |
| D7 | (2026-11-16) Humidity is a learning dimension from the start (dry air feels colder), accepting that buckets need more observations. |
| D8 | (2026-11-16) History backfill: past setpoint changes become a proposed seed the user reviews and accepts on the dashboard; recurring thermostat-schedule changes are filtered out. Room and humidity history is imported as context. |
| D9 | (2026-11-16) Phase 3 (rooms, humidity, backfill) ships before phase 2 (wake and bed times). |
| D10 | (2026-11-17) Learned wake and bed times are applied automatically, bounded: at most 15 minutes per week, at most 90 minutes from the configured time. Visible on the dashboard, with a reset. |
| D11 | (2026-11-17) Wake and bed times are learned from optional signal entities the user selects (lights, switches, buttons), with the occupant's thermostat changes as the fallback. |
| D12 | (2026-11-17) Weekday/weekend follows an optional Workday sensor (holidays are days off), for phase 1's day class too; without it, Saturday and Sunday. |
| D13 | (2026-11-17) "Analyse history" also proposes wake and bed times, reviewed and accepted like the temperature rows. |

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

## Phase 2: learned wake and bed times

Implements D2's wake and bed times and D10 to D13. Fork version `0.9.41.3`.

### Evidence from the target install (2026-11-17)

Recorder history, 2026-10-17 to 2026-11-17:

- Configured `wake_time` 06:30 and `sleep_time` 22:30 (the defaults).
- `light.bedroom` first turns on between 06:27 and 07:25 on workdays (median about 07:03) and between 06:40 and
  08:10 on days off (one 10:00). Its evening use (on about 17:00 to 19:30, off by 20:00) is not bedtime. It is a
  wake signal only.
- `light.tv_wled` (the TV LED) is turned on by an automation at 07:00:01 every day. It goes off by hand between 23:15
  and 23:56 on 9 of 31 nights and by an automation at exactly 23:59:00 on the others. On the late nights the downstairs
  switch's off button (`event.downstairs_switch_*`) is pressed between 00:12 and 00:50. Bedtime is therefore about
  23:15 to 00:50, roughly 80 minutes after the configured 22:30.
- Both of those trigger the "Good night" automation, which sets the Nest to 18 °C. That automation, not the Nest
  schedule, is the source of the "about 23:59 18.0 °C" slot in the phase 3 evidence, and of the 18 °C writes at
  varying times between 23:15 and 00:50.
- `binary_sensor.workday_sensor` exists (Workday integration; `workday.check_date` is available and returns a
  response).
- Home Assistant's history API returns states without context (`LazyState.context` is empty), so history cannot tell
  a person's change from an automation's. Live state-change events carry a context.

### Off by default

Everything in this phase requires `preference_learning_enabled`. With learning disabled, or with
`schedule_max_shift_min` at 0, wake and bed times are the configured ones, the wake and bed timers register exactly as
today, and every output is identical to phase 3. Without a workday entity the day class stays Saturday and Sunday.
All golden simulations stay unchanged.

### Settings (options flow step `preference_learning`)

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `workday_entity` | entity id or empty | empty | `binary_sensor` of the `workday` integration. Opt-in. |
| `wake_signal_entities` | list of entity ids | `[]` | `light`, `switch`, `input_boolean`, `binary_sensor`, `event`. |
| `bed_signal_entities` | list of entity ids | `[]` | Same domains. Help text: pick what you switch off or press when you go to bed. |
| `schedule_max_shift_min` | minutes | `90` | 0 to 180. 0 keeps the configured times fixed. |
| `schedule_weekly_step_min` | minutes | `15` | 5 to 60. |

Validation: a signal entity must exist; the same entity may be in both lists. The configured `wake_time` and
`sleep_time` stay the user's base; the learned shift is applied on top and shown next to them.

### Day class (D12, also changes phase 1)

With `workday_entity` set, a date's class is `weekday` when `workday.check_date` says it is a workday, otherwise
`weekend` (holidays are days off). The bucket key labels stay `weekday` and `weekend`, so stored phase 1 and 3 offsets
keep their meaning. Without it, or when the call fails (logged at WARNING once per date), Saturday and Sunday are
`weekend`. Results are cached per date in memory. The coordinator resolves every date it needs on the event loop:
today, tomorrow and, for a backfill, every date in the window before the executor job starts. Pure functions receive a
`{date: is_workday}` mapping and fall back to the weekday rule for a missing date. A night still belongs to the day it
wakes into.

### Effective schedule (one seam)

`learned_schedule` holds a signed shift in minutes per class and kind:
`{"weekday": {"wake": 0, "bed": 0}, "weekend": {"wake": 0, "bed": 0}}`.

- `effective_times(day)` returns the wake time on the morning of `day` (class of `day`) and the bedtime on the evening
  of `day` (class of `day + 1`, the day that night wakes into), each the configured time plus the shift clamped to
  `±schedule_max_shift_min`, as `"HH:MM"`, wrapping past midnight.
- `schedule_at(at)` returns the `WakeBed(wake, bed)` named tuple for the night that matters at `at`: before 12:00 the
  night that ends this morning (yesterday's bedtime and today's wake), from 12:00 the coming night (today's bedtime
  and tomorrow's wake). The existing `in_sleep_window` semantics apply unchanged to those times, including a bedtime at
  or after midnight (#1376, non-wrapping branch).
- Every reader of `wake_time` and `sleep_time` that means "the schedule at this moment" goes through `schedule_at`;
  forward-looking readers (chart band, plan, operation timeline, `predictor_v4` and `v4_shadow`, pre-cool fallback,
  briefing, next-automation text, `prediction_accuracy`) call it for each moment of their horizon, so a 24-hour horizon
  that crosses from a workday into a day off uses both. `predictor_v4.in_sleep_window` stays hass-free: it receives
  the pair. Readers that mean "the configured setting" (options flow, validation, settings display) keep the raw
  value. Grep every read (counts at 0.9.41.2: coordinator 53, automation 22, operation_timeline 18, v4_shadow 15,
  predictor_v4 14, preference_learning 11, prediction_accuracy 11, briefing 11, config_flow 8, const 7, v4_archive 5,
  preference_backfill 2, invariant_watchdog 2, api 2, ai_skills_context 2) and classify each one in the PR.
- Phase 1's part of day and the 60-minute wake and bed exclusion use the effective times.

### Timers

With phase 2 inactive the wake and bed callbacks stay registered with `async_track_time_change` at the configured
times, exactly as today (the golden simulations and HA-tier tests hook these). When active, they are scheduled with
`async_track_point_in_time` for the next effective wake and the next effective bedtime (local clock, DST-safe), and
re-armed after each fire and whenever the shift, the workday entity or the configured times change (weekly update,
seed accept, reset, options reload). A bedtime that moves past midnight fires once, on the right calendar day.

### Candidates

One candidate per class, kind and night at most:

- **Wake.** The first qualifying activation of a wake signal entity in `[configured wake − 3 h, configured wake + 3 h]`
  on that morning. Activation: a change to `on` for `light`, `switch`, `input_boolean` and `binary_sensor`; any new
  event for `event`.
- **Bed.** The last qualifying deactivation of a bed signal entity in `[configured bedtime − 3 h, configured bedtime +
  3 h]` of that night (a time after midnight belongs to the previous evening). Deactivation: a change to `off`; any new
  event for `event`.
- Only real transitions count: the old and new states are both known (not `unavailable` or `unknown`), so a restart or
  a dropped device never counts, and a history read's synthetic start-of-window state is skipped.
- **Not caused by an automation** (the shared predicate below, live only).
- Occupancy home or guest at the moment (live); history assumes home.
- **Thermostat fallback.** For a kind with no signal entities selected and automation on (phase 1 rule 2): the start
  of an occupant raise episode in the 60 minutes before the effective wake is a wake candidate; the start of an
  occupant lower episode in the 60 minutes before the effective bedtime is a bed candidate. These only move times
  earlier (once the heat is right nobody adjusts), which is why sensors are preferred.

Signal entities are observed in observe-only mode too: lights and switches are not confounded by Climate Advisor's
own writes.

A candidate is stored as `{"date", "class", "kind", "delta_min", "source": "sensor" | "thermostat" | "history"}` where
`date` is the waking date and `delta_min` is the signed distance from the configured time, wrapped to ±12 h. Retention
like observations (400 days, 2000 entries).

### Automation-caused changes (shared predicate)

A live state change is automation-caused when its context has a `parent_id`, or when a `call_service` event with a
`parent_id` context targeted that entity within the previous 120 seconds (targets resolved through entity, device and
area ids; the Nest's state update arrives through the cloud and often does not carry the caller's context). One
tracker, one predicate, used by the signal collector and by the live phase 1 to 3 observation path (an
automation-caused thermostat change is not an occupant override for learning; it may still be an override for the
rest of Climate Advisor, which is unchanged). History has no context: history candidates and history thermostat
changes rely on the schedule filter. For signal entities the filter uses ±2 minutes (automations fire to the second;
±10 minutes would also remove the person who turns the TV LED off at 23:54 near the automation's 23:59).

### Weekly update

Runs with the phase 1 weekly update. Per class and kind:

1. Take the most recent 20 candidates. Fewer than 4: no change.
2. `target = median(delta_min)`, clamped to `±schedule_max_shift_min`.
3. `new = current + clamp(target − current, ±schedule_weekly_step_min)`, clamped, rounded to 5 minutes.
4. Log a change at INFO and record it in `preference_history`. Re-arm the timers.

Lowering `schedule_max_shift_min` takes effect immediately (shifts are clamped when read).

### History backfill and seed

The existing "Analyse history" also reads the signal entities' history for the window, resolves workday for each date
first, applies the ±2-minute schedule filter, builds candidates (`source: history`) and adds one review row per class
and kind: n, median, current shift and the shift it would set. Rows with fewer than 4 candidates are shown disabled.
Accept applies the selected rows' shifts immediately (clamped to the maximum, not limited by the weekly step) and adds
the candidates to `schedule_candidates`; Discard drops them. Both re-arm the timers. A candidate already stored for the
same date, kind and source is not added twice.

### Persistence

New `LearningState` fields: `learned_schedule` and `schedule_candidates`. Validated in `load_state`; the
`preferences` reset scope clears them.

### Visibility

- Status tab: "Wake 07:05 on workdays (learned, +35 min) · Bed 23:50 (learned, +80 min)", with tomorrow's class.
- Analysis tab: per class and kind the configured time, shift, target, n and source, plus the most recent candidates.
- Briefing: tomorrow's wake time when it differs from the configured one.
- Diagnostics: the new state. All text through i18n in all 16 languages.

### Fixes to phases 1 and 3 in this PR

- The live observation path skips automation-caused thermostat changes (shared predicate).
- An HA-tier test against a real recorder pins that history states carry no context, so a future HA change that adds
  it is noticed (and the phase 3 note "rarely applies" becomes "never applies on this HA version").

### Tests

- Pure: `effective_times` and `schedule_at` across the noon pivot, Friday night and Sunday night with and without a
  workday mapping, a holiday, bedtime 23:59, 00:00 and 00:10 (sleep-window branch flip; `automation._in_sleep_window`
  and `predictor_v4.in_sleep_window` agree), a DST night (2026-10-25); candidate extraction (first on, last off, events,
  unavailable and unknown skipped, after-midnight bedtimes assigned to the previous evening); wrapped median; weekly
  step and clamp; ±2-minute schedule filter on the recorded TV LED pattern (07:00:01 on and 23:59:00 off removed, the
  23:15 to 23:56 hand offs kept).
- Engine and coordinator: timers registered as today when inactive; point-in-time timers fire at the effective times
  and re-arm after accept and the weekly update; a 24-hour chart horizon from a Friday afternoon uses both classes;
  the automation-caused predicate (parent context, and a `call_service` with a device target 30 s before) for the
  signal collector and the thermostat observation path; workday resolution on the loop before the backfill executor
  job; accept and discard.
- Golden simulations unchanged; the pending scenarios gain one where a learned 23:50 bedtime moves the setback.

### Implementation notes (phase 2, as built)

- A seed accept adds only the candidates of the rows the occupant selected. Candidates are deduplicated by (date, kind),
  stricter than the spec's per class, kind and night, because a night has one wake and one bedtime.
- Seed row keys are `"<class>|<kind>"`. A candidate's `delta_min` is validated to ±180 minutes (the maximum shift).
- Readers that look at a past night keep the pair stored at that moment: `v4_archive` uses the pair stored at `now`
  and `night_window` receives the pair at `now`. `compute_bedtime_setback` uses the configuration at `now`.
  `night_replay` validates each night against the wake and bed times effective on that night (`night_times_fn`).
- The history replay uses the current view with the weekday rule where a date has no resolved workday.
- The backfill has no thermostat-history fallback for a kind without signal entities (only live thermostat changes
  feed that fallback). The `user_id` keep rule in the backfill is left as it is (phase 3 scope).
- An `event` entity counts a new parseable timestamp even when its previous state is unknown, but only when that
  timestamp is within 60 seconds of now: the timestamp restored at startup is not a signal.
- "Caused by an automation" is a context test: `automation_triggered` and `script_started` contexts are remembered for
  120 seconds (bounded, pruned, unsubscribed on unload). A state change or service call whose context id or parent id
  is a remembered run, or whose service call has a parent id, is an automation's. This covers time-triggered
  automations, whose context has no parent.
- A reset (the dashboard button and the `reset_learning_data` service alike, through `async_reset_learning`) re-arms the
  wake and bed timers. `night_replay` rolls a bedtime earlier on the clock than the wake time (an after-midnight bedtime, learned or configured; the rule candidates and `instants_on` use) to the next calendar day. A failed workday lookup is retried after an hour and warns once per date.
- The seed review's Current column is the stored shift clamped to the current cap, as read.
- The weekly step starts from the current clamped shift, not the stored one (a lowered cap moves the step's base).
- The `preference_seed_accepted` message does not mention schedule rows; the API response carries `schedule` (the keys
  applied) instead.
- `homeassistant.helpers.target` (`TargetSelection`, `async_extract_referenced_entity_ids`) is verified on HA 2026.10
  only; the integration's minimum is 2026.3.0, where it is unchecked. The HA-tier test pins that recorder history
  carries no context.
- The Analysis tab's schedule table shows the configured time, shift, target and sample count; the signal source is
  listed on the recent-signal lines, not per row. Times in that table are 24-hour `HH:MM`.
- Open decision: thermostat fallback candidates only move times earlier (as specified); sensors are preferred.

## Phase 3: rooms, humidity and history backfill

Implements D3, D6, D7 and D8. Fork version `0.9.41.2`.

### Evidence from the target install (2026-11-16)

Recorder history of `climate.living_room` (30 days, 2026-10-17 to 2026-11-16, raw resolution) and the room sensors
was analysed before this spec was written:

- The Nest schedule is fixed and recognisable: 01:30 15.9, 06:15 16.0, 07:15 16.8, 08:45 15.9 and 21:30 16.1 °C,
  each within ±2 minutes on every day. A further 18.0 °C at about 23:59 (and at varying times from 23:15 to 00:50) is
  the "Good night" automation, not the Nest schedule (found in phase 2). After removing those, 85 occupant episodes remain (bursts of
  dial changes grouped with the phase 1 15-minute rule): morning raises to 19.5 to 21 °C, daytime raises to about
  20 °C, evening lowers to 16 to 17 °C.
- Room temperatures at the moment of a raise (hourly means): morning, thermostat MAD 0.59 °C (n=40), living room
  0.54, bedroom 0.42, child's bedroom 0.39; afternoon, thermostat 0.50 (n=11), child's bedroom 0.19 (n=9). The rooms
  move together; no room is a clear winner yet. The learned-room choice therefore needs a confidence gate and a
  manual pin per part of day.
- The Daikin room sensors report whole degrees and the Daikin units are independent heat sources in their rooms.
- The InfluxDB bucket (`ha_raw_90d`) would add about 60 days before 2026-10-17, which precede the heating season.
  The backfill reads the Home Assistant recorder only (in process, no credentials, raw resolution). InfluxDB is not
  used; revisit only if a recorder's retention is too short to cover a heating season.

### Off by default

Everything in this phase requires `preference_learning_enabled`. With no room selected and no humidity source, every
output is identical to phase 1 and all golden simulations stay unchanged.

### Settings (options flow step `preference_learning`)

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `room_temp_entities` | list of entity ids | `[]` | Multi-select, `sensor` with device class temperature (and `input_number`). Help text: do not pick a sensor built into another heater or AC unit. |
| `room_pin_night` / `room_pin_morning` / `room_pin_afternoon` / `room_pin_evening` | string | `"auto"` | `"auto"`, `"thermostat"`, or one of `room_temp_entities`. Validated against the selection; a stale entity reverts to `"auto"` with a warning log. |

Humidity needs no setting: each room's humidity is found with the existing Issue #1414 resolver
(`coordinator._humidity_role_inputs` and its same-device search), passing the room's temperature entity as the
source; when the room has no humidity sensor the indoor chain applies (configured `indoor_humidity_entity`, then the
climate entity's `current_humidity`).

### Steering reference per part of day

The **reference** for a moment is the entity whose temperature the comfort target is measured against:

1. In the sleep window, a configured `sleep_indoor_temp_entity` always wins (existing behaviour, explicit user choice).
2. Otherwise the part of day's pin: `"thermostat"` means none; an entity means that room.
3. `"auto"` uses the learned room for that part of day when one is confident (below), else the thermostat.

When the reference is a room and its reading is usable (present, numeric, plausible, not stale: the same checks as
`_resolve_sleep_indoor_temp_f`, generalised), `resolve_indoor_temp_with_provenance` returns that room's reading as the
comfort-facing value with a new context `REMOTE_CONTEXT_ROOM`, and the existing remote-offset outer loop
(`remote_offset.py`, `AutomationEngine.remote_offset_check` / `_remote_offset_for_write`) steers the thermostat
setpoint so that room reaches the target, with the same step, cap, startup wait and opposite-edge clamp it uses for
the sleep sensor. On any failure the thermostat reading is used, exactly as the sleep swap falls back today. The
reference is resolved by one pure function (`steering_reference(at, …)`) that every caller uses; no caller compares
`in_sleep_window` with `sleep_indoor_temp_entity` on its own any more.

Every place the sleep-only remote context reaches today must accept the room context. Known touchpoints (grep before
editing; counts at 0.9.41.1): `indoor_temp.py` (`resolve_indoor_temp_with_provenance`, `_resolve_sleep_indoor_temp_f`,
`REMOTE_CONTEXT_SLEEP`), `remote_offset.py` (`context_label`, `remote_offset_msg`, views), `automation.py`
(`remote_offset_check`, `_remote_offset_for_write`, 26 `in_sleep_window=` call sites), `coordinator.py` (26
`sleep_indoor_temp_entity`, 14 `in_sleep_window=`, `heat_offset_at`), `invariant_watchdog.py` (`heat_offset_at`,
`_armed_floor`, promises P1 and P7), `briefing.py`, `scheduler.py`, `operation_timeline.py`, `prediction_accuracy.py`,
`nat_vent_*`, `fan_*`, `ai_skills_context.py`, `api.py`, `frontend/nd_settings.js`. The watchdog must treat a room
context exactly like the sleep context (the phase 1 review found it disabling P1).

### Learned room

Per part of day (night, morning, afternoon, evening; day class and day type are not split, to keep enough data):

- **Raise samples.** When an episode with `final − old > 0` closes, the temperature of every selected room at episode
  open is stored with the observation (`rooms: {entity_id: temp_f}`, captured at open like `remote_f`).
- **Background samples.** At every 30-minute refresh, each selected room's usable reading is appended to a per room,
  per part-of-day reservoir (most recent 200 readings).
- **Score.** For a room with at least 10 raise samples in the part of day among the most recent 60:
  `ratio = MAD(raise temps) / MAD(background temps)` (MAD floor 0.1 °F). A low ratio means the occupants turn the heat
  up when that room reaches a consistent temperature, more consistently than that room normally varies. The
  thermostat's own reading is scored the same way.
- **Choice.** The room with the lowest ratio is chosen when its ratio is at most 0.7 and at least 0.15 lower than the
  thermostat's ratio. Otherwise the thermostat stays the reference. Sensors that report whole degrees (`_is_whole`)
  are scored and shown but never chosen automatically (resolution, and the Daikin case above); they can be pinned.
- Recomputed in the weekly update; a change of the chosen room is logged at INFO and added to `preference_history`.

### Reference frames for learned offsets

An observation's `delta_f` is only meaningful against the reference it was recorded under: the rooms on this install
differ by up to 2 °C from the thermostat. Each observation stores `ref` (the reference entity at episode open, `null`
for the thermostat). The weekly update and the seed only use observations whose `ref` equals the bucket's current
reference. When a part of day's reference changes, that part of day's offsets restart from 0 (logged); old
observations are kept and count again if the reference returns.

### Humidity dimension

- Band of the reference humidity at episode open: `dry` below 40 % RH, `normal` 40 to 60, `humid` above 60, `-` when
  no humidity is known. A band change needs the reading to pass the edge by 2 % RH (hysteresis against the band in
  force).
- The bucket key gains a fourth part: `day_type|part_of_day|day_class|band`. Phase 1 keys with three parts load as
  band `-`.
- Weekly update levels: the four-part key (at least 3 observations); else the band-agnostic three-part key across
  bands; else `day_type|part_of_day` across day classes and bands (the phase 1 fallback). Offsets are stored for each
  four-part key with observations and for each three-part key.
- Application looks up the current four-part key, then the three-part key, then 0, with the phase 1 clamps.

### History backfill and seed

Service `climate_advisor.backfill_preferences` (`entry_id`, `days` default 30, 1 to 400) and an "Analyse history"
button on the Analysis tab. Runs on the recorder executor (`recorder.get_instance(hass).async_add_executor_job`) and
never blocks the event loop.

1. Read from the recorder for the window: the climate entity's states (setpoint, `preset_mode`, mode, context), every
   selected room's temperature and humidity states, and the outdoor temperature (the configured outdoor sensor, else
   the weather entity's `temperature` attribute).
2. Keep changes with mode `heat` and no `eco` preset. A change whose context has a `user_id` was made in Home
   Assistant by a person and is always kept.
3. **Schedule filter.** A change is the thermostat's schedule when the same setpoint (±0.1 °C) is set within ±10
   minutes of the same clock time on at least 40 % of the days in the window and on at least 4 days. Unit-tested
   against the schedule above.
4. Group the rest into episodes (15 minutes), compute `old` and `final`, discard undos, apply the phase 1 60-minute
   wake and bed exclusion. Occupancy is not known historically; it is assumed home (stated in the UI).
5. For each episode: bucket from the date's day type (daily outdoor high through `classify_day`), part of day, day
   class and humidity band; `delta_f` against today's configured base floor for that moment; `ref = null`; room
   temperatures at episode open from the nearest reading at or before it (at most 30 minutes old). Background
   reservoirs are filled from the same room histories.
6. The result is stored as `preference_seed`: the observations (marked `"source": "history"`), a per-bucket summary
   (n, median delta, the offset it would set) and the window. It replaces any earlier unaccepted seed.

Review (Analysis tab): one row per bucket with n, median and resulting offset, a checkbox per row (default checked)
and Accept / Discard. **Accept applies the selected buckets' offsets immediately**, clamped to
`preference_max_offset_f` but not limited by the weekly step: accepting is an explicit user decision, and the UI says
"applies these offsets now". The accepted observations join `preference_observations`; later weekly updates treat
them like any other. Discard deletes the seed. Both are logged and recorded in `preference_history`.

### Persistence

New `LearningState` fields: `room_background` (`{part: {entity: [temp_f, …]}}`), `learned_rooms`
(`{part: {"entity": str|null, "ratios": {entity: float}, "n": int, "updated": date}}`), `preference_seed` (object or
null). Observations gain optional `rooms`, `ref`, `rh`, `band`, `source`. Validate in `load_state`; the `preferences`
reset scope clears all of them.

### Visibility

- Status tab: the thermostat-card line names the reference ("Steering by Bedroom (learned) 19.6 °C, thermostat
  +0.5 °C") and the humidity band.
- Analysis tab: per part of day the reference, its source (pinned, learned, sleep sensor, thermostat) and every
  candidate's ratio and n; the bucket table gains the band column; the seed review panel.
- Diagnostics: the new state (room entity ids are not secrets; nothing new to redact).
- Briefing: the learned-preference line names the room when the reference is a room.
- All text through i18n in all 16 languages.

### Tests

- Pure: `steering_reference` precedence (sleep sensor, pin, auto, fallback); learned-room score with the gate, the
  whole-degree exclusion and the MAD floor; band with hysteresis; four-part keys and the three-level fallback;
  three-part keys loading as band `-`; schedule filter on the recorded Nest schedule (all six slots removed, the
  occupant bursts kept); episode building from history; seed summary.
- Engine: the room context drives the remote-offset loop like the sleep context and falls back on a stale room;
  watchdog P1/P7 with a room context; offsets restart when the reference changes.
- Coordinator: observations carry `rooms`, `ref`, `rh`, `band`; backfill service reads the recorder off the event
  loop; accept applies offsets now within the cap; discard.
- Consistency: with a room reference and an offset active, chart band, briefing, Status line and written setpoint
  agree. Golden simulations unchanged.

### Implementation notes (phase 3, as built)

- `steering_reference` (pure, `preference_learning.py`) is the one source; the coordinator passes the room to
  `resolve_indoor_temp_with_provenance(room_entity=)` (`steering_room`), which sets `REMOTE_CONTEXT_ROOM` only while
  the thermostat heats (heat mode, or off on a heat-only unit; D5) and reports `room_reason` (`not_heating`, `stale`,
  `unavailable`) when it falls back. The watchdog's readings and heat offset come from the same reading and view, so
  P1/P7 parity with the sleep context holds by construction.
- Where the night is steered by a room instead of a sleep sensor, the operation timeline's plan holds the
  thermostat's room the room gap away from the band (`night_room_gap_f`), heat edge only (D5). The v4 predictor's
  node inputs, the v4 night strategy, the sleep-sensor health check and future `ca_swaps` breaks stay sleep-sensor
  only: v4 models the bedroom node, and a failing room is announced by the indoor-source note (`room_unusable`)
  rather than a health episode. Recorded `ca_swaps` rows already break on the room, and `prediction_accuracy` breaks
  on the chart log's recorded `band_sensor`, which already names the steering room. The chart's predicted-indoor line
  follows the v4 bedroom node; routing a room into it would mean modelling a per-room node in v4, out of proportion
  for this phase. Rooms steering the morning, afternoon or evening are not modelled in the plan; the band itself (the
  target) is the shared seam.
- No separate entity-health episodes for room sensors: a stale or unavailable room is a fall-back to the thermostat,
  logged on change, like the sleep sensor's.
- Seed rows for a part of day that a room steers are shown disabled and set no offset on Accept (their history
  deltas were measured at the thermostat, not in the room); their observations still join with `ref = null`, so they
  count once the thermostat steers that part again.
- Accept and Discard both keep the seed's room context: the background is merged into `room_background`, and raises
  with room temperatures that do not join as observations are kept with `context_only: true`. Context-only raises
  feed the learned-room score only, never an offset or the observation count; a later Accept of the same bucket turns
  them into observations. Accept requests a coordinator refresh so the new floors are armed at once. Analysing the
  same window again after a Discard repeats its background readings (a commented shortcut; each reservoir keeps its
  newest 200). Imported room temperatures and background readings are limited to the rooms selected at import (and
  the thermostat).
- No raise counts twice. Every observation now stores its episode `start`; an import skips an episode already stored
  under that start whatever its bucket or band (an observation from before 0.9.41.2 is identified by its close,
  `date` + `time`). The seed also stops where live recording took over (`live_since`: the earliest live episode start,
  the open episode included; the close of an older observation without `start`): a history episode that ends at or
  after 15 minutes before it is left out (`counts.live`), because it would have been part of that live episode.
- A seed without a valid window or counts is refused: nothing is stored, an earlier seed is kept, a WARNING is logged,
  the Analysis tab shows `preference_seed_invalid` (HTTP 422) and the service raises the translated
  `preference_seed_invalid` error.
- With a sleep sensor configured at analysis time, history changes in the sleep window are left out
  (`counts.sleep_sensor`) rather than tagged with the sleep sensor: that sensor's remote offset moved the setpoint the
  thermostat reported and the recorder does not keep the offset, so their delta is known in neither frame (reference
  frames: "only meaningful against the reference it was recorded under"). Live recording, which knows the offset,
  learns those nights.
- Background samples are taken at most once per 30 minutes per part of day (one minute of slack for a refresh that
  fires slightly early), not on every coordinator update; the humidity band and the room readings are refreshed on
  every update. The thermostat is sampled with the rooms so its ratio can be scored.
- An episode records the reference actually used at open: the room only while the room steered (`remote_context`
  set); a stale room or a cooling thermostat records `ref = null`. The same heat gate (mode heat, no eco preset)
  decides both live recording and the history filter.
- The first reconcile after the upgrade stamps each part, and its observations without a `ref`, with the phase 1
  frame they were recorded under (`references_by_part(config, None)`: the sleep sensor at night, else the
  thermostat). When the current reference differs (a pin set at upgrade), that part restarts like any other change.
  Reconcile also prunes background reservoirs of rooms no longer selected.
- Band levels: a band that appears for a bucket gets both its band row and a `-` row, each starting from the bucket's
  current learned offset (a three-part phase 1 key counts), never from 0. A `-` row is written only when the bucket
  also has a real band; an install without humidity keeps the three-part key alone (no mirror row). Application
  reads the four-part key, then the three-part key.
- Whole-degree detection (`is_whole_series`) judges a room's readings in the configured `temp_unit`, the unit every
  sensor state is read in (`read_sensor_state_f`). A sensor's native unit is not in its state: Home Assistant converts
  a whole-°C sensor on a °F system to 69.8, 71.6 °F, so that sensor is not recognised as whole there (it can still be
  pinned); on a °C system (the target install's Daikin sensors) it is. A 0.1 °C sensor reading 21.1 °C (69.98 °F) is
  never mistaken for a whole-°F one.
- The room picker accepts temperature sensors and `input_number` helpers (the remote reader reads both).
- Loading drops malformed learned rooms, seed rows, seed observations and background readings with a WARNING; a seed
  without a valid window or counts is dropped whole. A failed Accept or Discard returns an error to the Analysis tab
  (`preference_seed_failed`) instead of failing silently.
- The backfill analyses a deep copy of the config taken when it starts, so an options change during the run cannot
  mix two configurations.
- Recorder history rows carry no context, so the `user_id` keep rule never applies on this HA version (pinned by
  `tests_ha/test_history_has_no_context_realha.py`); the schedule filter does the work.
- The history humidity source is today's humidity source for the thermostat's room (entity or `entity#attribute`);
  when it is not usable, history observations get band `-`.
- Pins use a selector with `custom_value`, so a room added in the same form validates against the submitted
  selection.
- The briefing names a room only on a heating day (rooms never steer cooling).
- No lock against two concurrent backfills; the second replaces the first seed.
- The schedule filter keeps ±10 minutes. The test fixture adds the late 18.0 °C slot firing early on a few days
  (23:15, 23:18, 23:22, 23:45, 23:46, 23:54, 23:56): the ones within 10 minutes of the usual 23:59 are removed with
  the slot; the others survive as occupant episodes. With bedtime 23:00 they land in the night bucket with
  `delta_f = 0` against an 18 °C sleep floor, so they teach no offset. With a bedtime of 00:00 the 60-minute bed
  exclusion drops them all. With a sleep sensor configured they fall in the sleep window and are left out.
