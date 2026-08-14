"""Sensor entities for Presence Auto-Off room rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import PresenceAutoOffEntity
from .models import DayType, Status

if TYPE_CHECKING:
    from datetime import datetime

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
    """Set up sensors for a Presence Auto-Off room rule."""
    async_add_entities(
        (
            PresenceAutoOffStatusSensor(entry),
            PresenceAutoOffDayTypeSensor(entry),
            PresenceAutoOffNextShutdownSensor(entry),
            PresenceAutoOffLastShutdownSensor(entry),
            PresenceAutoOffLastRestorationSensor(entry),
        )
    )


class PresenceAutoOffStatusSensor(PresenceAutoOffEntity, SensorEntity):
    """Expose the current controller status."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = [status.value for status in Status]

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the status sensor."""
        super().__init__(entry, "status", "status")

    @property
    @override
    def native_value(self) -> str:
        """Return the current controller status."""
        return self.controller.status.value


class PresenceAutoOffDayTypeSensor(PresenceAutoOffEntity, SensorEntity):
    """Expose the day type calculated by the controller."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = [day_type.value for day_type in DayType]

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the day-type sensor."""
        super().__init__(entry, "day_type", "day_type")

    @property
    @override
    def native_value(self) -> str:
        """Return the current day classification."""
        return self.controller.day_type.value


class PresenceAutoOffNextShutdownSensor(PresenceAutoOffEntity, SensorEntity):
    """Expose the next scheduled automatic shutdown time."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the next-shutdown sensor."""
        super().__init__(entry, "next_shutdown", "next_shutdown")

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the active absence deadline, if one exists."""
        return self.controller.deadline


class PresenceAutoOffLastShutdownSensor(PresenceAutoOffEntity, SensorEntity):
    """Expose the time of the latest shutdown attempt."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the last-shutdown sensor."""
        super().__init__(entry, "last_shutdown", "last_shutdown")

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the latest execution timestamp, if available."""
        if (execution := self.controller.last_execution) is None:
            return None
        return execution.occurred_at


class PresenceAutoOffLastRestorationSensor(PresenceAutoOffEntity, SensorEntity):
    """Expose the time of the latest presence-triggered restoration."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the last-restoration sensor."""
        super().__init__(entry, "last_restoration", "last_restoration")

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the latest restoration timestamp, if available."""
        if (restoration := self.controller.last_restoration) is None:
            return None
        return restoration.occurred_at
