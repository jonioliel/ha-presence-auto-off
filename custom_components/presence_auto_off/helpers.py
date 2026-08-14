"""Small compatibility helpers for Presence Auto-Off."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_registry import RegistryEntry


@callback
def effective_area_id(hass: HomeAssistant, entity_entry: RegistryEntry) -> str | None:
    """Return an entity's own area or its device's inherited area."""
    if entity_entry.area_id is not None:
        return entity_entry.area_id
    if entity_entry.device_id is None:
        return None
    if (device_entry := dr.async_get(hass).async_get(entity_entry.device_id)) is None:
        return None
    return device_entry.area_id
