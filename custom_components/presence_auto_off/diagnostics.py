"""Diagnostics support for Presence Auto-Off."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import PresenceAutoOffConfigEntry


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant,
    entry: PresenceAutoOffConfigEntry,
) -> dict[str, Any]:
    """Return configuration and in-memory runtime diagnostics."""
    controller = entry.runtime_data
    return {
        "config_entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "runtime": controller.diagnostics,
    }
