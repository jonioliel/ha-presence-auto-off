"""Device metadata for Presence Auto-Off room rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_RULE_ID, DOMAIN

if TYPE_CHECKING:
    from . import PresenceAutoOffConfigEntry


def rule_device_info(
    entry: PresenceAutoOffConfigEntry,
    name: str,
) -> DeviceInfo:
    """Return the shared device definition for one room rule."""
    rule_id = str(entry.data[CONF_RULE_ID])
    return DeviceInfo(
        identifiers={(DOMAIN, rule_id)},
        configuration_url=(
            "homeassistant://config/integrations/integration/"
            f"{DOMAIN}#config_entry={entry.entry_id}"
        ),
        manufacturer="Presence Auto-Off",
        model="Room shutdown rule",
        name=name,
    )
