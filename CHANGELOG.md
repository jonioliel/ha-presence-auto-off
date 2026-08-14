# Changelog

All notable changes to Presence Auto-Off are documented here.

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

[1.1.0]: https://github.com/jonioliel/ha-presence-auto-off/releases/tag/v1.1.0
[1.0.0]: https://github.com/jonioliel/ha-presence-auto-off/releases/tag/v1.0.0
