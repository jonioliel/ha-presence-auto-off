"""Typed models for Presence Auto-Off."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Self

from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALLOWED_DAY_TYPES,
    CONF_AREA_ID,
    CONF_DELAY_SECONDS,
    CONF_HOLIDAY_ENTITY,
    CONF_NAME,
    CONF_PRESENCE_ENTITY,
    CONF_RULE_ID,
    CONF_SHABBAT_ENTITY,
    CONF_TARGET_ENTITIES,
    DEFAULT_ALLOWED_DAY_TYPES,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_NAME,
)


class Status(StrEnum):
    """Runtime status of a room rule."""

    INITIALIZING = "initializing"
    DISABLED = "disabled"
    OCCUPIED = "occupied"
    COUNTDOWN = "countdown"
    WAITING_CONDITION = "waiting_condition"
    EXECUTING = "executing"
    COMPLETED = "completed"
    SENSOR_UNAVAILABLE = "sensor_unavailable"
    ERROR = "error"


class DayType(StrEnum):
    """Day classification derived from the configured binary sensors."""

    ORDINARY = "ordinary"
    SHABBAT = "shabbat"
    HOLIDAY = "holiday"
    UNKNOWN = "unknown"


class ActivityEventType(StrEnum):
    """Activity emitted by a room rule."""

    EXECUTED = "executed"
    NO_ACTION = "no_action"
    BLOCKED = "blocked"
    FAILED = "failed"


ALL_DAY_TYPES = frozenset((DayType.ORDINARY, DayType.SHABBAT, DayType.HOLIDAY))


def _optional_string(value: Any) -> str | None:
    """Return a stripped optional string."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _string_sequence(value: Any) -> tuple[str, ...]:
    """Convert selector output to a de-duplicated tuple of strings."""
    if isinstance(value, str):
        values: Sequence[Any] = (value,)
    elif isinstance(value, Sequence):
        values = value
    else:
        return ()

    return tuple(
        dict.fromkeys(
            item.strip() for item in values if isinstance(item, str) and item.strip()
        )
    )


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a persisted UTC datetime."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt_util.parse_datetime(value)
    except ValueError:
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return dt_util.as_utc(parsed)


@dataclass(frozen=True, slots=True)
class RuleConfig:
    """Configuration for one room rule."""

    name: str
    area_id: str | None
    presence_entity: str
    delay_seconds: float
    target_entities: tuple[str, ...]
    shabbat_entity: str | None
    holiday_entity: str | None
    allowed_day_types: frozenset[DayType]
    rule_id: str = ""

    @classmethod
    def from_mapping(cls, options: Mapping[str, Any]) -> Self:
        """Build a rule configuration from config-entry data or options."""
        name = _optional_string(options.get(CONF_NAME)) or DEFAULT_NAME
        presence_entity = _optional_string(options.get(CONF_PRESENCE_ENTITY)) or ""

        raw_delay = options.get(CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS)
        if isinstance(raw_delay, bool):
            delay_seconds = float(DEFAULT_DELAY_SECONDS)
        else:
            try:
                parsed_delay = float(raw_delay)
                delay_seconds = (
                    max(0.0, parsed_delay)
                    if isfinite(parsed_delay)
                    else float(DEFAULT_DELAY_SECONDS)
                )
            except (TypeError, ValueError):
                delay_seconds = float(DEFAULT_DELAY_SECONDS)
        if not math.isfinite(delay_seconds):
            delay_seconds = float(DEFAULT_DELAY_SECONDS)

        raw_allowed = options.get(CONF_ALLOWED_DAY_TYPES, DEFAULT_ALLOWED_DAY_TYPES)
        allowed_values = _string_sequence(raw_allowed)
        allowed_day_types: set[DayType] = set()
        for value in allowed_values:
            try:
                day_type = DayType(value)
            except ValueError:
                continue
            if day_type is not DayType.UNKNOWN:
                allowed_day_types.add(day_type)

        return cls(
            name=name,
            area_id=_optional_string(options.get(CONF_AREA_ID)),
            presence_entity=presence_entity,
            delay_seconds=delay_seconds,
            target_entities=_string_sequence(options.get(CONF_TARGET_ENTITIES)),
            shabbat_entity=_optional_string(options.get(CONF_SHABBAT_ENTITY)),
            holiday_entity=_optional_string(options.get(CONF_HOLIDAY_ENTITY)),
            allowed_day_types=frozenset(allowed_day_types),
            rule_id=_optional_string(options.get(CONF_RULE_ID)) or "",
        )

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable configuration data."""
        data: dict[str, Any] = {
            CONF_NAME: self.name,
            CONF_AREA_ID: self.area_id,
            CONF_PRESENCE_ENTITY: self.presence_entity,
            CONF_DELAY_SECONDS: self.delay_seconds,
            CONF_TARGET_ENTITIES: list(self.target_entities),
            CONF_ALLOWED_DAY_TYPES: sorted(
                day_type.value for day_type in self.allowed_day_types
            ),
            CONF_RULE_ID: self.rule_id,
        }
        if self.shabbat_entity is not None:
            data[CONF_SHABBAT_ENTITY] = self.shabbat_entity
        if self.holiday_entity is not None:
            data[CONF_HOLIDAY_ENTITY] = self.holiday_entity
        return data


@dataclass(frozen=True, slots=True)
class AbsenceEpisode:
    """A continuous period for which absence has been observed."""

    episode_id: str
    generation: int
    presence_entity: str
    started_at: datetime
    deadline: datetime
    completed: bool = False
    completed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return persisted episode data."""
        return {
            "episode_id": self.episode_id,
            "generation": self.generation,
            "presence_entity": self.presence_entity,
            "started_at": self.started_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "completed": self.completed,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self | None:
        """Restore an episode, returning None for malformed data."""
        episode_id = _optional_string(data.get("episode_id"))
        presence_entity = _optional_string(data.get("presence_entity"))
        started_at = _parse_datetime(data.get("started_at"))
        deadline = _parse_datetime(data.get("deadline"))
        raw_generation = data.get("generation")
        if (
            episode_id is None
            or presence_entity is None
            or started_at is None
            or deadline is None
            or isinstance(raw_generation, bool)
            or not isinstance(raw_generation, int)
            or raw_generation < 0
        ):
            return None

        return cls(
            episode_id=episode_id,
            generation=raw_generation,
            presence_entity=presence_entity,
            started_at=started_at,
            deadline=deadline,
            completed=data.get("completed") is True,
            completed_at=_parse_datetime(data.get("completed_at")),
        )


@dataclass(frozen=True, slots=True)
class LastExecution:
    """Result of the latest target execution attempt."""

    episode_id: str
    occurred_at: datetime
    successful_entities: tuple[str, ...] = ()
    failed_entities: Mapping[str, str] = field(default_factory=dict)

    @property
    def partial_failure(self) -> bool:
        """Return whether some, but not all, target calls failed."""
        return bool(self.successful_entities and self.failed_entities)

    @property
    def succeeded(self) -> bool:
        """Return whether every attempted target call succeeded."""
        return not self.failed_entities

    @property
    def timestamp(self) -> datetime:
        """Return the execution timestamp (compatibility alias)."""
        return self.occurred_at

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable execution data."""
        return {
            "episode_id": self.episode_id,
            "occurred_at": self.occurred_at.isoformat(),
            "successful_entities": list(self.successful_entities),
            "failed_entities": dict(self.failed_entities),
            "partial_failure": self.partial_failure,
            "succeeded": self.succeeded,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self | None:
        """Restore an execution result, returning None if malformed."""
        episode_id = _optional_string(data.get("episode_id"))
        occurred_at = _parse_datetime(data.get("occurred_at"))
        if episode_id is None or occurred_at is None:
            return None

        failed_raw = data.get("failed_entities")
        failed_entities = (
            {
                entity_id: message
                for entity_id, message in failed_raw.items()
                if isinstance(entity_id, str) and isinstance(message, str)
            }
            if isinstance(failed_raw, Mapping)
            else {}
        )
        return cls(
            episode_id=episode_id,
            occurred_at=occurred_at,
            successful_entities=_string_sequence(data.get("successful_entities")),
            failed_entities=failed_entities,
        )


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """Observable runtime activity."""

    event_type: ActivityEventType
    occurred_at: datetime
    data: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable activity data."""
        return {
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.isoformat(),
            "data": dict(self.data),
        }


# Friendly aliases for consumers that prefer the more explicit names.
ControllerStatus = Status
ActivityType = ActivityEventType
