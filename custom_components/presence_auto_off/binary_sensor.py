"""Binary sensor entities for Presence Auto-Off room rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory

from .entity import PresenceAutoOffEntity
from .models import Status

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
    async_add_entities(
        (
            PresenceAutoOffShutdownAllowedBinarySensor(entry),
            PresenceAutoOffProblemBinarySensor(entry),
        )
    )


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


class PresenceAutoOffProblemBinarySensor(PresenceAutoOffEntity, BinarySensorEntity):
    """Expose an actionable fault separately from ordinary room occupancy."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the room problem sensor."""
        super().__init__(entry, "problem", "problem")

    @property
    @override
    def is_on(self) -> bool:
        """Flag unavailable targets or failed execution in an enabled rule."""
        if not self.controller.enabled or self.controller.status is Status.INITIALIZING:
            return False
        return bool(self.controller.target_issues) or self.controller.status in (
            Status.ERROR,
            Status.SENSOR_UNAVAILABLE,
        )

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose fault details suitable for notification automations."""
        execution = self.controller.last_execution
        return {
            **super().extra_state_attributes,
            "rule_enabled": self.controller.enabled,
            "rule_status": self.controller.status.value,
            "presence_entity": self.controller.config.presence_entity,
            "target_issues": self.controller.target_issues,
            "last_failed_entities": (
                dict(execution.failed_entities) if execution is not None else {}
            ),
        }
