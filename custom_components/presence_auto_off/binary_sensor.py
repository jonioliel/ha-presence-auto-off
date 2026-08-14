"""Binary sensor entities for Presence Auto-Off room rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import PresenceAutoOffEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import (
        AddConfigEntryEntitiesCallback,
    )

    from . import PresenceAutoOffConfigEntry


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: PresenceAutoOffConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the shutdown-allowed sensor for a room rule."""
    async_add_entities((PresenceAutoOffShutdownAllowedBinarySensor(entry),))


class PresenceAutoOffShutdownAllowedBinarySensor(
    PresenceAutoOffEntity, BinarySensorEntity
):
    """Report whether the configured day gate currently permits shutdown."""

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the shutdown-allowed binary sensor."""
        super().__init__(entry, "shutdown_allowed", "shutdown_allowed")

    @property
    @override
    def is_on(self) -> bool:
        """Return whether the current day classification is allowed."""
        return self.controller.gate_allowed
