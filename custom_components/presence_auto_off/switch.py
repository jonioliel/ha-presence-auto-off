"""Switch entities for Presence Auto-Off room rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.switch import SwitchEntity

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
    """Set up the enable switch for a Presence Auto-Off room rule."""
    async_add_entities((PresenceAutoOffEnabledSwitch(entry),))


class PresenceAutoOffEnabledSwitch(PresenceAutoOffEntity, SwitchEntity):
    """Allow users and automations to enable or pause a room rule."""

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the automatic-shutdown switch."""
        super().__init__(entry, "enabled", "enabled")

    @property
    @override
    def is_on(self) -> bool:
        """Return whether the room rule is enabled."""
        return self.controller.enabled

    @override
    async def async_turn_on(self, **_kwargs: object) -> None:
        """Enable automatic shutdown for this room rule."""
        await self.controller.async_set_enabled(True)

    @override
    async def async_turn_off(self, **_kwargs: object) -> None:
        """Pause automatic shutdown for this room rule."""
        await self.controller.async_set_enabled(False)
