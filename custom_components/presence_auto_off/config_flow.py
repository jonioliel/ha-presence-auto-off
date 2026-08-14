"""Config flow for Presence Auto-Off."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, override
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import SERVICE_TURN_OFF
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    DurationSelector,
    DurationSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
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
    DEFAULT_DELAY_SECONDS,
    DEFAULT_RESTORE_ON_PRESENCE,
    DOMAIN,
)
from .helpers import effective_area_id
from .models import DayType

CONF_DELAY = "delay"
MAX_DELAY_SECONDS = 30 * 24 * 60 * 60
ALL_DAY_TYPES = [
    DayType.ORDINARY.value,
    DayType.SHABBAT.value,
    DayType.HOLIDAY.value,
]

_BINARY_SENSOR_SELECTOR = EntitySelector(
    EntitySelectorConfig(filter={"domain": "binary_sensor"})
)
_DAY_TYPES_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=ALL_DAY_TYPES,
        multiple=True,
        mode=SelectSelectorMode.DROPDOWN,
        translation_key="day_type",
    )
)


def _seconds_to_duration(seconds: float) -> dict[str, float]:
    """Convert seconds to the duration-selector shape."""
    remaining = int(seconds)
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, remaining = divmod(remaining, 60)
    result: dict[str, float] = {}
    if days:
        result["days"] = days
    if hours:
        result["hours"] = hours
    if minutes:
        result["minutes"] = minutes
    if remaining or not result:
        result["seconds"] = remaining
    return result


def _duration_to_seconds(value: Mapping[str, float]) -> float:
    """Convert duration-selector data to seconds."""
    return timedelta(
        days=value.get("days", 0),
        hours=value.get("hours", 0),
        minutes=value.get("minutes", 0),
        seconds=value.get("seconds", 0),
        milliseconds=value.get("milliseconds", 0),
    ).total_seconds()


@callback
def _resolve_entity_id(
    hass: HomeAssistant, entity_id_or_uuid: str | None
) -> str | None:
    """Resolve an entity selector value to a current entity ID."""
    if not entity_id_or_uuid:
        return None
    return er.async_resolve_entity_id(er.async_get(hass), entity_id_or_uuid)


@callback
def _resolve_entity_ids(
    hass: HomeAssistant, entity_ids_or_uuids: list[str]
) -> tuple[list[str], bool]:
    """Resolve selector values and report whether any could not be resolved."""
    resolved: list[str] = []
    unresolved = False
    for entity_id_or_uuid in entity_ids_or_uuids:
        entity_id = _resolve_entity_id(hass, entity_id_or_uuid)
        if entity_id is None:
            unresolved = True
        elif entity_id not in resolved:
            resolved.append(entity_id)
    return resolved, unresolved


@callback
def _display_entity_id(
    hass: HomeAssistant, entity_id_or_uuid: str | None
) -> str | None:
    """Return the current entity ID used by frontend entity pickers."""
    if not entity_id_or_uuid:
        return None
    return _resolve_entity_id(hass, entity_id_or_uuid) or entity_id_or_uuid


@callback
def _display_entity_ids(
    hass: HomeAssistant, entity_ids_or_uuids: list[str]
) -> list[str]:
    """Return de-duplicated current IDs for a multiple entity picker."""
    displayed: list[str] = []
    for entity_id_or_uuid in entity_ids_or_uuids:
        entity_id = _display_entity_id(hass, entity_id_or_uuid)
        if entity_id is not None and entity_id not in displayed:
            displayed.append(entity_id)
    return displayed


@callback
def _canonical_entity_reference(hass: HomeAssistant, entity_id: str) -> str:
    """Return a rename-safe registry reference when one is available."""
    registry_entry = er.async_get(hass).async_get(entity_id)
    return registry_entry.id if registry_entry is not None else entity_id


@callback
def _resolve_binary_sensor(
    hass: HomeAssistant, entity_id_or_uuid: str | None
) -> tuple[str | None, str | None]:
    """Resolve and validate an optional binary-sensor selector value."""
    if not entity_id_or_uuid:
        return None, None
    entity_id = _resolve_entity_id(hass, entity_id_or_uuid)
    if entity_id is None:
        return None, "entity_not_found"
    if split_entity_id(entity_id)[0] != "binary_sensor":
        return None, "invalid_binary_sensor"
    return entity_id, None


@callback
def _turn_off_entities_in_area(
    hass: HomeAssistant,
    area_id: str,
    selected: list[str] | None = None,
) -> list[str]:
    """Return enabled area entities whose domain offers turn_off."""
    entity_registry = er.async_get(hass)

    candidates = {
        entry.entity_id
        for entry in entity_registry.entities.values()
        if not entry.disabled
        and effective_area_id(hass, entry) == area_id
        and hass.services.has_service(
            split_entity_id(entry.entity_id)[0], SERVICE_TURN_OFF
        )
    }

    # Keep existing choices visible if the entity was moved, disabled, or is
    # temporarily unavailable. Runtime validation remains fail-safe.
    resolved_selected, _ = _resolve_entity_ids(hass, selected or [])
    candidates.update(resolved_selected)
    return sorted(candidates)


def _optional_entity_marker(key: str, value: str | None) -> vol.Optional:
    """Build an optional field that can also be cleared by the user."""
    if value:
        return vol.Optional(key, description={"suggested_value": value})
    return vol.Optional(key)


def _required_marker(key: str, value: str | None) -> vol.Required:
    """Build a required field with a default only when one exists."""
    if value:
        return vol.Required(key, default=value)
    return vol.Required(key)


class _RuleFlowMixin:
    """Shared forms used by initial setup and options."""

    hass: HomeAssistant
    _working: dict[str, Any]

    def _room_schema(self) -> vol.Schema:
        presence_entity = _display_entity_id(
            self.hass, self._working.get(CONF_PRESENCE_ENTITY)
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=self._working.get(CONF_NAME, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                _required_marker(
                    CONF_AREA_ID, self._working.get(CONF_AREA_ID)
                ): AreaSelector(),
                _required_marker(
                    CONF_PRESENCE_ENTITY,
                    presence_entity,
                ): _BINARY_SENSOR_SELECTOR,
            }
        )

    def _targets_schema(self, candidates: list[str]) -> vol.Schema:
        """Return a safely restricted target selector schema."""
        if not candidates:
            # EntitySelector treats an empty include_entities list as no filter,
            # which would expose every entity in Home Assistant.
            return vol.Schema({})

        selected = _display_entity_ids(
            self.hass, list(self._working.get(CONF_TARGET_ENTITIES, []))
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_TARGET_ENTITIES,
                    default=selected,
                ): EntitySelector(
                    EntitySelectorConfig(
                        include_entities=candidates,
                        multiple=True,
                        reorder=True,
                    )
                )
            }
        )

    def _conditions_schema(self) -> vol.Schema:
        shabbat = _display_entity_id(self.hass, self._working.get(CONF_SHABBAT_ENTITY))
        holiday = _display_entity_id(self.hass, self._working.get(CONF_HOLIDAY_ENTITY))
        return vol.Schema(
            {
                vol.Required(
                    CONF_DELAY,
                    default=_seconds_to_duration(
                        self._working.get(CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS)
                    ),
                ): DurationSelector(
                    DurationSelectorConfig(enable_day=True, enable_second=True)
                ),
                _optional_entity_marker(
                    CONF_SHABBAT_ENTITY, shabbat
                ): _BINARY_SENSOR_SELECTOR,
                _optional_entity_marker(
                    CONF_HOLIDAY_ENTITY, holiday
                ): _BINARY_SENSOR_SELECTOR,
                vol.Required(
                    CONF_ALLOWED_DAY_TYPES,
                    default=list(
                        self._working.get(CONF_ALLOWED_DAY_TYPES, ALL_DAY_TYPES)
                    ),
                ): _DAY_TYPES_SELECTOR,
                vol.Required(
                    CONF_RESTORE_ON_PRESENCE,
                    default=(
                        self._working.get(
                            CONF_RESTORE_ON_PRESENCE,
                            DEFAULT_RESTORE_ON_PRESENCE,
                        )
                        is True
                    ),
                ): BooleanSelector(),
            }
        )

    def _accept_room(self, user_input: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        previous_area = self._working.get(CONF_AREA_ID)
        name = user_input[CONF_NAME].strip()
        if not name:
            errors[CONF_NAME] = "name_required"
        presence_reference = user_input.get(CONF_PRESENCE_ENTITY)
        presence_entity, presence_error = _resolve_binary_sensor(
            self.hass, presence_reference
        )
        if presence_entity is None and presence_error is None:
            presence_error = "entity_not_found"
        if presence_error is not None:
            errors[CONF_PRESENCE_ENTITY] = presence_error
        if errors:
            return errors

        assert presence_entity is not None
        self._working.update(user_input)
        self._working[CONF_NAME] = name
        if previous_area is not None and previous_area != user_input[CONF_AREA_ID]:
            # Targets are an explicit safety boundary. Never carry selections
            # from an old room into a newly selected area.
            self._working.pop(CONF_TARGET_ENTITIES, None)
        self._working[CONF_PRESENCE_ENTITY] = _canonical_entity_reference(
            self.hass, presence_entity
        )
        return errors

    def _accept_targets(self, user_input: dict[str, Any]) -> dict[str, str]:
        raw_targets = list(dict.fromkeys(user_input.get(CONF_TARGET_ENTITIES, [])))
        targets, unresolved = _resolve_entity_ids(self.hass, raw_targets)
        if unresolved:
            return {CONF_TARGET_ENTITIES: "entity_not_found"}
        if not targets:
            return {CONF_TARGET_ENTITIES: "targets_required"}

        allowed_targets = set(
            _turn_off_entities_in_area(
                self.hass,
                self._working[CONF_AREA_ID],
            )
        )
        if not set(targets) <= allowed_targets:
            return {CONF_TARGET_ENTITIES: "target_not_allowed"}

        self._working[CONF_TARGET_ENTITIES] = [
            _canonical_entity_reference(self.hass, entity_id) for entity_id in targets
        ]
        return {}

    def _accept_conditions(self, user_input: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        delay_seconds = _duration_to_seconds(user_input[CONF_DELAY])
        if delay_seconds < 0 or delay_seconds > MAX_DELAY_SECONDS:
            errors[CONF_DELAY] = "invalid_delay"

        allowed = list(user_input[CONF_ALLOWED_DAY_TYPES])
        if not allowed:
            errors[CONF_ALLOWED_DAY_TYPES] = "day_type_required"

        shabbat_reference = user_input.get(CONF_SHABBAT_ENTITY)
        holiday_reference = user_input.get(CONF_HOLIDAY_ENTITY)
        shabbat, shabbat_error = _resolve_binary_sensor(self.hass, shabbat_reference)
        holiday, holiday_error = _resolve_binary_sensor(self.hass, holiday_reference)
        if shabbat_error is not None:
            errors[CONF_SHABBAT_ENTITY] = shabbat_error
        if holiday_error is not None:
            errors[CONF_HOLIDAY_ENTITY] = holiday_error
        if shabbat and holiday and shabbat == holiday:
            errors[CONF_HOLIDAY_ENTITY] = "same_gate_entity"
        if set(allowed) != set(ALL_DAY_TYPES) and not (shabbat or holiday):
            errors[CONF_ALLOWED_DAY_TYPES] = "gate_required"

        if errors:
            return errors

        self._working[CONF_DELAY_SECONDS] = delay_seconds
        self._working[CONF_ALLOWED_DAY_TYPES] = allowed
        self._working[CONF_RESTORE_ON_PRESENCE] = (
            user_input.get(
                CONF_RESTORE_ON_PRESENCE,
                DEFAULT_RESTORE_ON_PRESENCE,
            )
            is True
        )
        self._working.pop(CONF_SHABBAT_ENTITY, None)
        self._working.pop(CONF_HOLIDAY_ENTITY, None)
        if shabbat is not None:
            self._working[CONF_SHABBAT_ENTITY] = _canonical_entity_reference(
                self.hass, shabbat
            )
        if holiday is not None:
            self._working[CONF_HOLIDAY_ENTITY] = _canonical_entity_reference(
                self.hass, holiday
            )
        return {}


class PresenceAutoOffConfigFlow(_RuleFlowMixin, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a room rule."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._working = {}

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PresenceAutoOffOptionsFlow:
        """Return the options flow."""
        return PresenceAutoOffOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect room and presence details."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._accept_room(user_input)
            if not errors:
                return await self.async_step_targets()
        return self.async_show_form(
            step_id="user", data_schema=self._room_schema(), errors=errors
        )

    async def async_step_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select explicit targets from the chosen area."""
        errors: dict[str, str] = {}
        candidates = _turn_off_entities_in_area(
            self.hass,
            self._working[CONF_AREA_ID],
            self._working.get(CONF_TARGET_ENTITIES),
        )
        if not candidates:
            errors["base"] = "no_supported_entities"
        elif user_input is not None:
            errors = self._accept_targets(user_input)
            if not errors:
                return await self.async_step_conditions()
        return self.async_show_form(
            step_id="targets",
            data_schema=self._targets_schema(candidates),
            errors=errors,
        )

    async def async_step_conditions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the delay and day-type gate."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._accept_conditions(user_input)
            if not errors:
                rule_id = uuid4().hex
                await self.async_set_unique_id(rule_id)
                return self.async_create_entry(
                    title=self._working[CONF_NAME],
                    data={CONF_RULE_ID: rule_id},
                    options=self._working,
                )
        return self.async_show_form(
            step_id="conditions",
            data_schema=self._conditions_schema(),
            errors=errors,
        )


class PresenceAutoOffOptionsFlow(_RuleFlowMixin, OptionsFlowWithReload):
    """Edit a room rule and reload it once."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._working = {}

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit room and presence details."""
        if not self._working:
            self._working = dict(self.config_entry.options)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._accept_room(user_input)
            if not errors:
                return await self.async_step_targets()
        return self.async_show_form(
            step_id="init", data_schema=self._room_schema(), errors=errors
        )

    async def async_step_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit explicit targets."""
        errors: dict[str, str] = {}
        candidates = _turn_off_entities_in_area(
            self.hass,
            self._working[CONF_AREA_ID],
            self._working.get(CONF_TARGET_ENTITIES),
        )
        if not candidates:
            errors["base"] = "no_supported_entities"
        elif user_input is not None:
            errors = self._accept_targets(user_input)
            if not errors:
                return await self.async_step_conditions()
        return self.async_show_form(
            step_id="targets",
            data_schema=self._targets_schema(candidates),
            errors=errors,
        )

    async def async_step_conditions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit delay and day-type gate."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._accept_conditions(user_input)
            if not errors:
                if self.config_entry.title != self._working[CONF_NAME]:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, title=self._working[CONF_NAME]
                    )
                return self.async_create_entry(title="", data=self._working)
        return self.async_show_form(
            step_id="conditions",
            data_schema=self._conditions_schema(),
            errors=errors,
        )
