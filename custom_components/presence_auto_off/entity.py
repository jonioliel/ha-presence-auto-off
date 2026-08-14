"""Shared entity support for Presence Auto-Off."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import PresenceAutoOffConfigEntry
from .const import CONF_AREA_ID, CONF_NAME, CONF_RULE_ID, DOMAIN
from .controller import PresenceAutoOffController


class PresenceAutoOffEntity(Entity):
    """Base class for entities belonging to one room rule."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: PresenceAutoOffConfigEntry,
        key: str,
        translation_key: str,
    ) -> None:
        """Initialize a rule entity."""
        self.controller: PresenceAutoOffController = entry.runtime_data
        rule_id = str(entry.data[CONF_RULE_ID])
        self._attr_unique_id = f"{rule_id}_{key}"
        self._attr_translation_key = translation_key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, rule_id)},
            manufacturer="Presence Auto-Off",
            model="Room shutdown rule",
            name=str(self.controller.config.name),
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe after the entity is registered."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.controller.async_add_listener(self._async_controller_updated)
        )

    @callback
    def _async_controller_updated(self) -> None:
        """Write the latest controller state."""
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose stable rule context for troubleshooting."""
        return {
            CONF_AREA_ID: self.controller.config.area_id,
            CONF_NAME: self.controller.config.name,
        }
