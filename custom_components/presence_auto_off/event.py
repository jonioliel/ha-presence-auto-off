"""Event entities for Presence Auto-Off room rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from homeassistant.components.event import EventEntity
from homeassistant.core import callback

from .const import ATTR_OCCURRED_AT
from .entity import PresenceAutoOffEntity
from .models import ActivityEventType

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import (
        AddConfigEntryEntitiesCallback,
    )

    from . import PresenceAutoOffConfigEntry
    from .models import ActivityEvent


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: PresenceAutoOffConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the activity event entity for a room rule."""
    async_add_entities((PresenceAutoOffActivityEvent(entry),))


class PresenceAutoOffActivityEvent(PresenceAutoOffEntity, EventEntity):
    """Publish controller activity as a Home Assistant event entity."""

    _attr_event_types: ClassVar[list[str]] = [
        event_type.value for event_type in ActivityEventType
    ]

    def __init__(self, entry: PresenceAutoOffConfigEntry) -> None:
        """Initialize the activity event entity."""
        super().__init__(entry, "activity", "activity")

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to controller activity after entity registration."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.controller.async_add_activity_listener(self._async_handle_activity)
        )

    @callback
    def _async_handle_activity(self, activity: ActivityEvent) -> None:
        """Publish a controller activity record."""
        event_data = dict(activity.data)
        event_data[ATTR_OCCURRED_AT] = activity.occurred_at.isoformat()
        self._trigger_event(activity.event_type.value, event_data)
        self.async_write_ha_state()

    @callback
    @override
    def _async_controller_updated(self) -> None:
        """Ignore regular updates; only activity creates an event state."""
