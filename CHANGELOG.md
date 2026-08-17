# Changelog

All notable changes to SmplWise Presence Auto-Off are documented here.

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
