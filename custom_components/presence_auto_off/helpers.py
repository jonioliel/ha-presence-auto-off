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
    if device_entry.area_id is not None:
        return device_entry.area_id
    # HA child devices inherit their parent's area unless the entity or child
    # overrides it. Older HA versions have no parent_device_id attribute.
    parent_id = getattr(device_entry, "parent_device_id", None)
    if parent_id is not None:
        parent = dr.async_get(hass).async_get(parent_id)
        if parent is not None:
            return parent.area_id
    return None
