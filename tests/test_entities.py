"""Tests for rule devices and their generated entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import SERVICE_TURN_OFF, STATE_ON
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.presence_auto_off.const import (
    CONF_ALLOWED_DAY_TYPES,
    CONF_AREA_ID,
    CONF_DELAY_SECONDS,
    CONF_NAME,
    CONF_PRESENCE_ENTITY,
    CONF_RESTORE_ON_PRESENCE,
    CONF_RULE_ID,
    CONF_TARGET_ENTITIES,
    DOMAIN,
)
from custom_components.presence_auto_off.models import DayType

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall


async def test_rule_device_owns_every_generated_entity(
    hass: HomeAssistant,
) -> None:
    """A room rule is an integration device, not a detached helper."""

    async def async_turn_off(_call: ServiceCall) -> None:
        """Provide the target service used by controller validation."""

    hass.services.async_register("light", SERVICE_TURN_OFF, async_turn_off)

    area = ar.async_get(hass).async_create("Test bedroom")
    entity_registry = er.async_get(hass)
    presence = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "device-test-presence",
        suggested_object_id="device_test_presence",
    )
    target = entity_registry.async_get_or_create(
        "light",
        "test",
        "device-test-light",
        suggested_object_id="device_test_light",
    )
    entity_registry.async_update_entity(target.entity_id, area_id=area.id)
    hass.states.async_set(presence.entity_id, STATE_ON)
    hass.states.async_set(target.entity_id, STATE_ON)

    rule_id = "device-test-rule"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test bedroom",
        unique_id=rule_id,
        data={CONF_RULE_ID: rule_id},
        options={
            CONF_NAME: "Test bedroom",
            CONF_AREA_ID: area.id,
            CONF_PRESENCE_ENTITY: presence.id,
            CONF_TARGET_ENTITIES: [target.id],
            CONF_DELAY_SECONDS: 300,
            CONF_ALLOWED_DAY_TYPES: [
                day.value for day in DayType if day is not DayType.UNKNOWN
            ],
            CONF_RESTORE_ON_PRESENCE: False,
        },
    )
    entry.add_to_hass(hass)

    # Reproduce an entity registry entry created by an older release without
    # a device link. Platform setup must repair it instead of leaving the new
    # rule device empty.
    detached_status = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{rule_id}_status",
        config_entry=entry,
        device_id=None,
        has_entity_name=True,
        original_name="Status",
        suggested_object_id="detached_rule_status",
    )
    assert detached_status.device_id is None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, rule_id),
        entry.entry_id,
    )
    assert device is not None
    assert device.config_entry_id == entry.entry_id
    assert device.configuration_url == (
        "homeassistant://config/integrations/integration/"
        f"{DOMAIN}#config_entry={entry.entry_id}"
    )

    rule_entities = er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    )
    assert {entity.unique_id for entity in rule_entities} == {
        f"{rule_id}_activity",
        f"{rule_id}_day_type",
        f"{rule_id}_enabled",
        f"{rule_id}_last_restoration",
        f"{rule_id}_last_shutdown",
        f"{rule_id}_next_shutdown",
        f"{rule_id}_shutdown_allowed",
        f"{rule_id}_status",
    }
    assert {entity.device_id for entity in rule_entities} == {device.id}
    assert all(entity.platform == DOMAIN for entity in rule_entities)

    assert await hass.config_entries.async_unload(entry.entry_id)
