# Changelog

## 1.3.4

- Retry an unconfirmed shutdown once immediately when the original device state and all safety conditions still hold; otherwise retain the configured retry schedule.

All notable changes to SmplWise Presence Auto-Off are documented here.

## [1.3.3] - 2026-11-25

### Fixed

- Retain a positive-interval retry when saving the shutdown boundary fails,
  including setup, startup, resume and re-enable, without sending device commands.
- Rearm periodic enforcement after unexpected execution interruptions and record
  uncertain targets as failed instead of leaving a room without its next check.
- Preserve failed-check status across presence attribute updates while waiting
  for the next enforcement interval.

### Added

- Expose current target validation problems and previous execution failures on
  the room status sensor, and timer subscription state in diagnostics.
- Observe target health even when presence restoration is disabled, without
  advancing or resetting the room countdown.
- Add a diagnostic problem binary sensor per room for unavailable targets,
  unavailable required sensors and execution errors. Intentionally disabled rules
  and normal day restrictions do not raise a problem.

## [1.3.2] - 2026-11-23

### Fixed

- Space target shutdowns by half a second and recheck presence and rule
  eligibility after the pause before continuing with the next target.
- Add regression coverage for an initially empty room with remote activation
  and a fifteen-minute enforcement interval, without any presence transition.

## [1.3.1] - 2026-11-02

### Fixed

- Allow up to five seconds for device state reports after a successful service
  call, for both shutdown and restoration. A shutdown is successful only after
  Home Assistant confirms `off`, even when restoration is disabled.
- Preserve stored countdowns and restoration journals while Home Assistant is
  starting; reconcile only after startup finishes.
- Rearm positive enforcement intervals after cancelled execution, retaining
  uncertain restore records without replaying them; zero-delay rules stay one-shot.
- Honor areas inherited from parent devices in newer Home Assistant versions.
- Exclude Presence/Runtime Auto-Off control switches from target selection and
  execution, preventing one rule from disabling itself or another rule.
- Export diagnostics for disabled or not-yet-loaded rules without raising an
  error when no runtime controller exists.
- Register devices without depending on `DeviceInfo` being a dictionary.

### Development

- Add regression coverage and a weekly Linux CI matrix for the minimum supported
  HA test baseline, pinned HA 2026.10.4, and the latest available test helper.

## [1.3.0] - 2026-08-17

### Added

- While presence remains `off`, the rule now checks the explicitly selected
  targets again after every configured no-presence interval.
- A target turned on remotely in an empty room is turned off at the next
  interval, subject to the same enable, day-gate, area, identity, and
  availability checks.

### Changed

- Targets that are already off are detected without sending redundant
  `turn_off` service calls.
- The next-check deadline survives reloads and restarts, and presence returning
  cancels the recurring enforcement immediately.

## [1.2.2] - 2026-08-14

### Fixed

- Anchored every shutdown deadline to the presence sensor's actual transition to
  `off`, including setup and reload reconciliation.
- Confirmed that attribute-only sensor updates do not restart the countdown and
  that completed absence episodes never behave as a repeating interval.

### Changed

- Clarified the current-absence and last-executed shutdown entity names.

## [1.2.1] - 2026-08-14

### Changed

- Branded the integration as SmplWise Presence Auto-Off.
- Room-rule devices now identify their manufacturer as SmplWise (SW).

## [1.2.0] - 2026-08-14

### Fixed

- Room rules are now registered as first-class integration devices instead of helpers.
- Every generated status, control, and activity entity is attached to its room-rule device.
- Each room-rule device includes a Home Assistant configuration link back to its highlighted config entry.

## [1.1.0] - 2026-08-14

### Added

- Optional, restart-safe restoration of the prior state of explicitly selected entities when presence returns.
- Per-target restore safety checks, write-ahead persistence, timeout isolation, and restoration activity/status reporting.
- Last-restoration timestamp sensor.

### Changed

- Clarified in the config flow that the Area only filters candidates and that control requires an explicit entity selection, with a warning for group entities.
- Entity selectors now display current entity IDs after renames while configuration remains stored with stable registry references.
- Shabbat and holiday sensors are more clearly presented as optional day-gate inputs for both shutdown and restoration.

## [1.0.0] - 2026-08-14

### Added

- UI-based room rules with Shelly Presence and generic binary-sensor support.
- Explicit, area-scoped selection of entities that provide `turn_off`.
- Configurable absence delays and weekday, Shabbat, and holiday gating.
- Restart-safe countdowns with fail-safe handling of unavailable sensors.
- Status, day-type, next-run, last-run, enabled, allowed, and activity entities.
- Hebrew and English translations, diagnostics, local branding, and HACS metadata.

[1.2.2]: https://github.com/jonioliel/ha-presence-auto-off/releases/tag/v1.2.2
[1.3.0]: https://github.com/jonioliel/ha-presence-auto-off/releases/tag/v1.3.0
[1.2.1]: https://github.com/jonioliel/ha-presence-auto-off/releases/tag/v1.2.1
[1.2.0]: https://github.com/jonioliel/ha-presence-auto-off/releases/tag/v1.2.0
[1.1.0]: https://github.com/jonioliel/ha-presence-auto-off/releases/tag/v1.1.0
[1.0.0]: https://github.com/jonioliel/ha-presence-auto-off/releases/tag/v1.0.0
