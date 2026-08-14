"""Constants for the Presence Auto-Off integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "presence_auto_off"

CONF_NAME: Final = "name"
CONF_AREA_ID: Final = "area_id"
CONF_PRESENCE_ENTITY: Final = "presence_entity"
CONF_DELAY_SECONDS: Final = "delay_seconds"
CONF_TARGET_ENTITIES: Final = "target_entities"
CONF_SHABBAT_ENTITY: Final = "shabbat_entity"
CONF_HOLIDAY_ENTITY: Final = "holiday_entity"
CONF_ALLOWED_DAY_TYPES: Final = "allowed_day_types"
CONF_RULE_ID: Final = "rule_id"

DEFAULT_NAME: Final = "Presence Auto-Off"
DEFAULT_DELAY_SECONDS: Final = 600
DEFAULT_ENABLED: Final = True
DEFAULT_ALLOWED_DAY_TYPES: Final[tuple[str, str, str]] = (
    "ordinary",
    "shabbat",
    "holiday",
)

STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = DOMAIN

EVENT_ACTIVITY: Final = f"{DOMAIN}_activity"
ATTR_ENTRY_ID: Final = "entry_id"
ATTR_RULE_ID: Final = "rule_id"
ATTR_EVENT_TYPE: Final = "event_type"
ATTR_OCCURRED_AT: Final = "occurred_at"
ATTR_DATA: Final = "data"
