<!-- Nav: ← [Preference Learning](preference-learning-spec.md) -->

# Preference Learning handoff (fork Dunky13#1)

Updated 2026-11-17 with phase 2 (PR #4, 0.9.41.3).

## State

| Phase | What | Release | Status |
|-------|------|---------|--------|
| 1 | Learned comfort offsets per weather, part of day, weekday/weekend | 0.9.41.1 | shipped |
| 3 | Room sensors, humidity bands, history backfill and seed review | 0.9.41.2 | shipped |
| 2 | Learned workday and day-off wake and bed times | 0.9.41.3 | PR #4 |

Everything is off until `preference_learning_enabled` is turned on. The target install runs observe-only
(`switch.living_room_automation` off), so Climate Advisor writes nothing to the Nest.

## Setup on the target install (owner steps)

1. Options → Preference learning: enable it; pick the room sensors (phase 3); set the workday entity
   `binary_sensor.workday_sensor`; wake signal `light.bedroom`; bed signals `light.tv_wled` and the downstairs
   switch's off-button event entity (only the off button, so "on" presses downstairs do not count).
2. Analysis tab → "Analyse history": review the temperature rows and the wake/bed rows. On the 30 days before
   2026-11-17 the expected proposals are about +35 minutes for wake (06:30 → about 07:05) and about +80 minutes for
   bed (22:30 → about 23:50). Untick anything that looks wrong, then Accept.
3. Before turning automation on: the "Good night" automation sets the Nest to 18 °C at bedtime. Phase 2 no longer
   learns from it, but the rest of Climate Advisor treats that write as a manual override. Either remove that action
   from "Good night" or keep it knowing Climate Advisor's own sleep setpoint is also 18 °C (64.4 °F).
4. Turn off the Nest's own schedule and eco features if Climate Advisor should drive the setpoint, then turn
   `switch.living_room_automation` on.

## Evidence and checks

- Phase 2 spec, decisions D10–D13 and as-built notes: [preference-learning-spec.md](preference-learning-spec.md).
- PR #4 lists the local validation on its head commit, the two review rounds and the reader classification.
- Hosted CI runs the Node i18n conformance check (needs Node 22.12.0 / ICU 76.1), which could not run locally.

## Known limits and next steps

- Translations of the new strings in 15 languages are unreviewed machine output.
- No dashboard screenshots: the preview browser times out on this Home Assistant's pages.
- History cannot tell an automation's change from a person's (no context in recorder history); only fixed-time
  automations are filtered there. Live learning excludes automation-caused changes.
- Thermostat-only wake/bed learning can only move times earlier; signal entities are the reliable source.
- Open: native-speaker review of the translations; checking the Analysis tab's seed review in a real browser.
