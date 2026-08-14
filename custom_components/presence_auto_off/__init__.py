"""Presence Auto-Off integration."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_HOLIDAY_ENTITY,
    CONF_PRESENCE_ENTITY,
    CONF_SHABBAT_ENTITY,
    CONF_TARGET_ENTITIES,
)
from .controller import PresenceAutoOffController
from .device import rule_device_info
from .models import RuleConfig

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type PresenceAutoOffConfigEntry = ConfigEntry[PresenceAutoOffController]


def _resolve_runtime_config(
    hass: HomeAssistant, entry: PresenceAutoOffConfigEntry
) -> dict[str, Any]:
    """Merge entry data/options and resolve rename-safe entity references."""
    config: dict[str, Any] = {**entry.data, **entry.options}
    entity_registry = er.async_get(hass)

    def resolve(reference: str) -> str:
        try:
            return er.async_validate_entity_id(entity_registry, reference)
        except vol.Invalid:
            # Preserve stale references so runtime status reports a safe
            # unavailable/failure state instead of aborting entry setup.
            return reference

    for key in (
        CONF_PRESENCE_ENTITY,
        CONF_SHABBAT_ENTITY,
        CONF_HOLIDAY_ENTITY,
    ):
        if isinstance(reference := config.get(key), str):
            config[key] = resolve(reference)

    if isinstance(targets := config.get(CONF_TARGET_ENTITIES), list):
        config[CONF_TARGET_ENTITIES] = [
            resolve(reference) if isinstance(reference, str) else reference
            for reference in targets
        ]

    return config


async def async_setup_entry(
    hass: HomeAssistant, entry: PresenceAutoOffConfigEntry
) -> bool:
    """Set up a room rule from a config entry."""
    controller = PresenceAutoOffController(
        hass,
        entry.entry_id,
        RuleConfig.from_mapping(_resolve_runtime_config(hass, entry)),
    )
    entry.runtime_data = controller

    # Register the rule as a first-class integration device before its
    # platforms load. Entity device_info uses the same stable identifier, so
    # every generated entity is attached to this exact device. Explicit
    # registration also keeps the device visible when users disable entities.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **rule_device_info(entry, controller.config.name),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    try:
        await controller.async_setup()
    except Exception:
        with suppress(Exception):
            await controller.async_unload()
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: PresenceAutoOffConfigEntry
) -> bool:
    """Unload a room rule and all of its listeners."""
    controller = entry.runtime_data
    await controller.async_stop()
    try:
        platforms_unloaded = await hass.config_entries.async_unload_platforms(
            entry, PLATFORMS
        )
    except BaseException:
        await controller.async_resume()
        raise
    if not platforms_unloaded:
        await controller.async_resume()
        return False
    await controller.async_unload()
    return True
