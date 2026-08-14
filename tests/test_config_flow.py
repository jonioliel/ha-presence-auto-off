"""Config-flow tests for Presence Auto-Off."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import SERVICE_TURN_OFF, STATE_ON
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.presence_auto_off import (
    async_setup_entry as integration_setup_entry,
)
from custom_components.presence_auto_off.config_flow import (
    ALL_DAY_TYPES,
    CONF_DELAY,
)
from custom_components.presence_auto_off.const import (
    CONF_ALLOWED_DAY_TYPES,
    CONF_AREA_ID,
    CONF_DELAY_SECONDS,
    CONF_HOLIDAY_ENTITY,
    CONF_NAME,
    CONF_PRESENCE_ENTITY,
    CONF_RESTORE_ON_PRESENCE,
    CONF_RULE_ID,
    CONF_SHABBAT_ENTITY,
    CONF_TARGET_ENTITIES,
    DEFAULT_RESTORE_ON_PRESENCE,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall


def _schema_marker(result: dict[str, Any], key: str) -> Any:
    """Return a voluptuous marker from a flow result by field name."""
    return next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", marker) == key
    )


def _schema_default(result: dict[str, Any], key: str) -> Any:
    """Return a field default from a flow result."""
    return _schema_marker(result, key).default()


def _schema_suggested_value(result: dict[str, Any], key: str) -> Any:
    """Return an optional field's suggested value from a flow result."""
    return _schema_marker(result, key).description["suggested_value"]


@pytest.mark.parametrize(
    ("restore_on_presence", "expected_restore"),
    [(None, False), (True, True)],
)
async def test_user_flow_limits_targets_to_selected_area(
    hass: HomeAssistant,
    restore_on_presence: bool | None,
    expected_restore: bool,
) -> None:
    """The setup wizard rejects controllable entities from another area."""

    async def async_turn_off(_call: ServiceCall) -> None:
        """Provide the service capability used by area candidate discovery."""

    hass.services.async_register("light", SERVICE_TURN_OFF, async_turn_off)

    area_registry = ar.async_get(hass)
    room = area_registry.async_create("Test room")
    other_room = area_registry.async_create("Other room")
    entity_registry = er.async_get(hass)

    presence = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "room-presence",
        suggested_object_id="room_presence",
    )
    presence = entity_registry.async_update_entity(presence.entity_id, area_id=room.id)
    room_light = entity_registry.async_get_or_create(
        "light",
        "test",
        "room-light",
        suggested_object_id="room_light",
    )
    room_light = entity_registry.async_update_entity(
        room_light.entity_id, area_id=room.id
    )
    other_light = entity_registry.async_get_or_create(
        "light",
        "test",
        "other-light",
        suggested_object_id="other_light",
    )
    other_light = entity_registry.async_update_entity(
        other_light.entity_id, area_id=other_room.id
    )
    shabbat = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "shabbat",
        suggested_object_id="shabbat",
    )
    holiday = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "holiday",
        suggested_object_id="holiday",
    )
    hass.states.async_set(presence.entity_id, STATE_ON)

    with patch(
        "custom_components.presence_auto_off.async_setup_entry",
        wraps=integration_setup_entry,
    ) as mock_setup_entry:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Test room",
                CONF_AREA_ID: room.id,
                CONF_PRESENCE_ENTITY: presence.entity_id,
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "targets"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_TARGET_ENTITIES: [other_light.id]},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "targets"
        assert result["errors"] == {CONF_TARGET_ENTITIES: "target_not_allowed"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_TARGET_ENTITIES: [
                    room_light.entity_id,
                    room_light.id,
                ]
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "conditions"
        assert (
            _schema_default(result, CONF_RESTORE_ON_PRESENCE)
            is DEFAULT_RESTORE_ON_PRESENCE
        )

        conditions_input: dict[str, Any] = {
            CONF_DELAY: {"minutes": 5},
            CONF_ALLOWED_DAY_TYPES: ALL_DAY_TYPES,
            CONF_SHABBAT_ENTITY: shabbat.entity_id,
            CONF_HOLIDAY_ENTITY: holiday.entity_id,
        }
        if restore_on_presence is not None:
            conditions_input[CONF_RESTORE_ON_PRESENCE] = restore_on_presence
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], conditions_input
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Test room"

        entry = result["result"]
        assert entry.data[CONF_RULE_ID] == entry.unique_id
        assert entry.options[CONF_AREA_ID] == room.id
        assert entry.options[CONF_PRESENCE_ENTITY] == presence.id
        assert entry.options[CONF_TARGET_ENTITIES] == [room_light.id]
        assert entry.options[CONF_SHABBAT_ENTITY] == shabbat.id
        assert entry.options[CONF_HOLIDAY_ENTITY] == holiday.id
        assert entry.options[CONF_DELAY_SECONDS] == 300
        assert entry.options[CONF_ALLOWED_DAY_TYPES] == ALL_DAY_TYPES
        assert entry.options[CONF_RESTORE_ON_PRESENCE] is expected_restore

        await hass.async_block_till_done()
        mock_setup_entry.assert_awaited_once()


async def test_empty_area_never_renders_an_unrestricted_target_selector(
    hass: HomeAssistant,
) -> None:
    """An empty candidate set cannot become an unrestricted entity picker."""
    area = ar.async_get(hass).async_create("Empty room")
    entity_registry = er.async_get(hass)
    presence = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "empty-room-presence",
        suggested_object_id="empty_room_presence",
    )
    presence = entity_registry.async_update_entity(presence.entity_id, area_id=area.id)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Empty room",
            CONF_AREA_ID: area.id,
            CONF_PRESENCE_ENTITY: presence.entity_id,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "targets"
    assert result["errors"] == {"base": "no_supported_entities"}
    assert result["data_schema"].schema == {}


@pytest.mark.parametrize(
    ("stored_restore", "expected_default", "submitted_restore", "keep_gates"),
    [(None, False, True, False), (True, True, False, True)],
)
async def test_options_display_current_ids_and_persist_registry_references(
    hass: HomeAssistant,
    stored_restore: bool | None,
    expected_default: bool,
    submitted_restore: bool,
    keep_gates: bool,
) -> None:
    """Renamed entity references render cleanly and remain rename-safe."""

    async def async_turn_off(_call: ServiceCall) -> None:
        """Expose turn_off for target discovery."""

    hass.services.async_register("light", SERVICE_TURN_OFF, async_turn_off)

    area = ar.async_get(hass).async_create("Rename-safe room")
    entity_registry = er.async_get(hass)
    presence = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "rename-presence",
        suggested_object_id="rename_presence",
    )
    presence = entity_registry.async_update_entity(presence.entity_id, area_id=area.id)
    target = entity_registry.async_get_or_create(
        "light",
        "test",
        "rename-target",
        suggested_object_id="rename_target",
    )
    target = entity_registry.async_update_entity(target.entity_id, area_id=area.id)
    shabbat = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "rename-shabbat",
        suggested_object_id="rename_shabbat",
    )
    holiday = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "rename-holiday",
        suggested_object_id="rename_holiday",
    )

    options: dict[str, Any] = {
        CONF_NAME: "Rename-safe room",
        CONF_AREA_ID: area.id,
        CONF_PRESENCE_ENTITY: presence.id,
        CONF_TARGET_ENTITIES: [target.id],
        CONF_DELAY_SECONDS: 300,
        CONF_SHABBAT_ENTITY: shabbat.id,
        CONF_HOLIDAY_ENTITY: holiday.id,
        CONF_ALLOWED_DAY_TYPES: ALL_DAY_TYPES,
    }
    if stored_restore is not None:
        options[CONF_RESTORE_ON_PRESENCE] = stored_restore

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rename-safe room",
        unique_id="rename-safe-rule",
        data={CONF_RULE_ID: "rename-safe-rule"},
        options=options,
    )
    entry.add_to_hass(hass)

    presence = entity_registry.async_update_entity(
        presence.entity_id,
        new_entity_id="binary_sensor.renamed_presence",
    )
    target = entity_registry.async_update_entity(
        target.entity_id,
        new_entity_id="light.renamed_target",
    )
    shabbat = entity_registry.async_update_entity(
        shabbat.entity_id,
        new_entity_id="binary_sensor.renamed_shabbat",
    )
    holiday = entity_registry.async_update_entity(
        holiday.entity_id,
        new_entity_id="binary_sensor.renamed_holiday",
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert _schema_default(result, CONF_PRESENCE_ENTITY) == presence.entity_id

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Rename-safe room",
            CONF_AREA_ID: area.id,
            CONF_PRESENCE_ENTITY: presence.entity_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "targets"
    assert _schema_default(result, CONF_TARGET_ENTITIES) == [target.entity_id]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_TARGET_ENTITIES: [target.entity_id]},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "conditions"
    assert _schema_suggested_value(result, CONF_SHABBAT_ENTITY) == shabbat.entity_id
    assert _schema_suggested_value(result, CONF_HOLIDAY_ENTITY) == holiday.entity_id
    assert _schema_default(result, CONF_RESTORE_ON_PRESENCE) is expected_default

    conditions_input: dict[str, Any] = {
        CONF_DELAY: {"minutes": 5},
        CONF_ALLOWED_DAY_TYPES: ALL_DAY_TYPES,
        CONF_RESTORE_ON_PRESENCE: submitted_restore,
    }
    if keep_gates:
        conditions_input[CONF_SHABBAT_ENTITY] = shabbat.entity_id
        conditions_input[CONF_HOLIDAY_ENTITY] = holiday.entity_id
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], conditions_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PRESENCE_ENTITY] == presence.id
    assert entry.options[CONF_TARGET_ENTITIES] == [target.id]
    if keep_gates:
        assert entry.options[CONF_SHABBAT_ENTITY] == shabbat.id
        assert entry.options[CONF_HOLIDAY_ENTITY] == holiday.id
    else:
        assert CONF_SHABBAT_ENTITY not in entry.options
        assert CONF_HOLIDAY_ENTITY not in entry.options
    assert entry.options[CONF_RESTORE_ON_PRESENCE] is submitted_restore


async def test_options_area_change_rejects_target_from_previous_area(
    hass: HomeAssistant,
) -> None:
    """Changing room cannot preserve a target belonging to the old room."""

    async def async_turn_off(_call: ServiceCall) -> None:
        """Expose turn_off so both room lights are selectable candidates."""

    hass.services.async_register("light", SERVICE_TURN_OFF, async_turn_off)

    area_registry = ar.async_get(hass)
    old_area = area_registry.async_create("Old room")
    new_area = area_registry.async_create("New room")
    entity_registry = er.async_get(hass)

    old_presence = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "old-presence",
        suggested_object_id="old_presence",
    )
    old_presence = entity_registry.async_update_entity(
        old_presence.entity_id, area_id=old_area.id
    )
    new_presence = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "new-presence",
        suggested_object_id="new_presence",
    )
    new_presence = entity_registry.async_update_entity(
        new_presence.entity_id, area_id=new_area.id
    )
    old_light = entity_registry.async_get_or_create(
        "light",
        "test",
        "old-light",
        suggested_object_id="old_light",
    )
    old_light = entity_registry.async_update_entity(
        old_light.entity_id, area_id=old_area.id
    )
    new_light = entity_registry.async_get_or_create(
        "light",
        "test",
        "new-light",
        suggested_object_id="new_light",
    )
    entity_registry.async_update_entity(new_light.entity_id, area_id=new_area.id)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Old room",
        unique_id="area-change-rule",
        data={CONF_RULE_ID: "area-change-rule"},
        options={
            CONF_NAME: "Old room",
            CONF_AREA_ID: old_area.id,
            CONF_PRESENCE_ENTITY: old_presence.id,
            CONF_TARGET_ENTITIES: [old_light.id],
            CONF_DELAY_SECONDS: 300,
            CONF_ALLOWED_DAY_TYPES: ALL_DAY_TYPES,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "New room",
            CONF_AREA_ID: new_area.id,
            CONF_PRESENCE_ENTITY: new_presence.id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "targets"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_TARGET_ENTITIES: [old_light.id]},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "targets"
    assert result["errors"] == {CONF_TARGET_ENTITIES: "target_not_allowed"}
