"""Typed models for Presence Auto-Off."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from homeassistant.util import dt as dt_util

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
    DEFAULT_ALLOWED_DAY_TYPES,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_NAME,
    DEFAULT_RESTORE_ON_PRESENCE,
)


class Status(StrEnum):
    """Runtime status of a room rule."""

    INITIALIZING = "initializing"
    DISABLED = "disabled"
    OCCUPIED = "occupied"
    COUNTDOWN = "countdown"
    WAITING_CONDITION = "waiting_condition"
    EXECUTING = "executing"
    RESTORING = "restoring"
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
    RESTORED = "restored"
    RESTORE_SKIPPED = "restore_skipped"
    RESTORE_FAILED = "restore_failed"


class RestoreItemPhase(StrEnum):
    """Durable phase of one target in an active restore transaction."""

    PREPARED = "prepared"
    READY = "ready"
    RESTORING = "restoring"


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


def _copy_json_value(value: Any) -> Any:
    """Return an independent JSON-safe value or raise ValueError."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            copied[key] = _copy_json_value(item)
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_copy_json_value(item) for item in value]
    raise ValueError(f"Value of type {type(value).__name__} is not JSON safe")


def _copy_json_mapping(value: Any) -> dict[str, Any]:
    """Return an independent JSON-safe object or raise ValueError."""
    if not isinstance(value, Mapping):
        raise ValueError("Expected a JSON object")
    try:
        copied = _copy_json_value(value)
    except RecursionError as err:
        raise ValueError("Cyclic JSON data is not supported") from err
    assert isinstance(copied, dict)
    return copied


def _strict_string_mapping(value: Any) -> dict[str, str] | None:
    """Copy a string mapping, rejecting malformed keys or values."""
    if not isinstance(value, Mapping):
        return None
    copied: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _optional_string(raw_key)
        if key is None or not isinstance(raw_value, str):
            return None
        if key in copied:
            return None
        copied[key] = raw_value[:500]
    return copied


def _strict_string_sequence(value: Any) -> tuple[str, ...] | None:
    """Copy a sequence of unique non-empty strings or reject it."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    copied: list[str] = []
    for raw_item in value:
        item = _optional_string(raw_item)
        if item is None or item in copied:
            return None
        copied.append(item)
    return tuple(copied)


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
    restore_on_presence: bool = DEFAULT_RESTORE_ON_PRESENCE
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
                    if math.isfinite(parsed_delay)
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

        restore_on_presence = (
            options.get(CONF_RESTORE_ON_PRESENCE, DEFAULT_RESTORE_ON_PRESENCE) is True
        )

        return cls(
            name=name,
            area_id=_optional_string(options.get(CONF_AREA_ID)),
            presence_entity=presence_entity,
            delay_seconds=delay_seconds,
            target_entities=_string_sequence(options.get(CONF_TARGET_ENTITIES)),
            shabbat_entity=_optional_string(options.get(CONF_SHABBAT_ENTITY)),
            holiday_entity=_optional_string(options.get(CONF_HOLIDAY_ENTITY)),
            allowed_day_types=frozenset(allowed_day_types),
            restore_on_presence=restore_on_presence,
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
            CONF_RESTORE_ON_PRESENCE: self.restore_on_presence,
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
class EntityStateSnapshot:
    """JSON-safe state captured immediately around an automatic shutdown."""

    state: str
    attributes: Mapping[str, Any]
    captured_at: datetime

    def __post_init__(self) -> None:
        """Normalize time and detach all nested attribute values."""
        if not isinstance(self.state, str) or not self.state:
            raise ValueError("A snapshot state is required")
        if (
            not isinstance(self.captured_at, datetime)
            or self.captured_at.tzinfo is None
        ):
            raise ValueError("Snapshot time must be timezone-aware")
        object.__setattr__(self, "attributes", _copy_json_mapping(self.attributes))
        object.__setattr__(self, "captured_at", dt_util.as_utc(self.captured_at))

    def as_dict(self) -> dict[str, Any]:
        """Return an independent JSON-serializable snapshot."""
        return {
            "state": self.state,
            "attributes": _copy_json_mapping(self.attributes),
            "captured_at": self.captured_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self | None:
        """Restore a snapshot, rejecting malformed or non-JSON attributes."""
        if not isinstance(data, Mapping):
            return None
        state = data.get("state")
        captured_at = _parse_datetime(data.get("captured_at"))
        if not isinstance(state, str) or not state or captured_at is None:
            return None
        try:
            return cls(
                state=state,
                attributes=_copy_json_mapping(data.get("attributes")),
                captured_at=captured_at,
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class RestoreItem:
    """Durable restore transaction state for one selected registry entity."""

    registry_entry_id: str
    entity_id_at_capture: str
    before: EntityStateSnapshot
    phase: RestoreItemPhase
    after_off: EntityStateSnapshot | None = None
    modified_since_off: bool = False

    def __post_init__(self) -> None:
        """Validate phase invariants and stable entity identity."""
        registry_entry_id = _optional_string(self.registry_entry_id)
        entity_id = _optional_string(self.entity_id_at_capture)
        if registry_entry_id is None or entity_id is None:
            raise ValueError("Restore item entity identity is required")
        if not isinstance(self.before, EntityStateSnapshot):
            raise ValueError("Restore item requires a before snapshot")
        if not isinstance(self.phase, RestoreItemPhase):
            raise ValueError("Restore item phase is invalid")
        if self.after_off is not None and not isinstance(
            self.after_off, EntityStateSnapshot
        ):
            raise ValueError("Restore item after-off snapshot is invalid")
        if self.phase is RestoreItemPhase.PREPARED and self.after_off is not None:
            raise ValueError("A prepared item cannot have an after-off snapshot")
        if self.phase is not RestoreItemPhase.PREPARED and self.after_off is None:
            raise ValueError("A ready restore item requires an after-off snapshot")
        if not isinstance(self.modified_since_off, bool):
            raise ValueError("Restore modification marker must be boolean")
        object.__setattr__(self, "registry_entry_id", registry_entry_id)
        object.__setattr__(self, "entity_id_at_capture", entity_id)

    @property
    def registry_id(self) -> str:
        """Return the stable entity-registry ID (concise compatibility alias)."""
        return self.registry_entry_id

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable transaction data."""
        return {
            "registry_entry_id": self.registry_entry_id,
            "entity_id_at_capture": self.entity_id_at_capture,
            "before": self.before.as_dict(),
            "phase": self.phase.value,
            "after_off": (
                self.after_off.as_dict() if self.after_off is not None else None
            ),
            "modified_since_off": self.modified_since_off,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self | None:
        """Restore an item, rejecting unknown phases and broken invariants."""
        if not isinstance(data, Mapping):
            return None
        registry_entry_id = _optional_string(data.get("registry_entry_id"))
        entity_id = _optional_string(data.get("entity_id_at_capture"))
        before_raw = data.get("before")
        after_raw = data.get("after_off")
        raw_modified = data.get("modified_since_off", False)
        if (
            registry_entry_id is None
            or entity_id is None
            or not isinstance(before_raw, Mapping)
            or not isinstance(raw_modified, bool)
        ):
            return None
        try:
            phase = RestoreItemPhase(data.get("phase"))
        except (TypeError, ValueError):
            return None
        before = EntityStateSnapshot.from_dict(before_raw)
        after_off = (
            EntityStateSnapshot.from_dict(after_raw)
            if isinstance(after_raw, Mapping)
            else None
        )
        if before is None or (after_raw is not None and after_off is None):
            return None
        try:
            return cls(
                registry_entry_id=registry_entry_id,
                entity_id_at_capture=entity_id,
                before=before,
                phase=phase,
                after_off=after_off,
                modified_since_off=raw_modified,
            )
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """Persisted restore work tied to one continuous absence episode."""

    episode_id: str
    presence_entity: str
    created_at: datetime
    day_type_at_shutdown: DayType
    items: tuple[RestoreItem, ...]

    def __post_init__(self) -> None:
        """Normalize immutable fields and reject ambiguous entity identity."""
        episode_id = _optional_string(self.episode_id)
        presence_entity = _optional_string(self.presence_entity)
        if episode_id is None or presence_entity is None:
            raise ValueError("Restore plan episode and presence identity are required")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("Restore plan time must be timezone-aware")
        if not isinstance(self.day_type_at_shutdown, DayType):
            raise ValueError("Restore plan day type is invalid")
        if isinstance(self.items, (str, bytes, bytearray)):
            raise ValueError("Restore plan items must be a sequence")
        items = tuple(self.items)
        if not items or not all(isinstance(item, RestoreItem) for item in items):
            raise ValueError("Restore plan requires at least one valid item")
        registry_ids = {item.registry_entry_id for item in items}
        entity_ids = {item.entity_id_at_capture for item in items}
        if len(registry_ids) != len(items) or len(entity_ids) != len(items):
            raise ValueError("Restore plan contains duplicate entity identity")
        object.__setattr__(self, "episode_id", episode_id)
        object.__setattr__(self, "presence_entity", presence_entity)
        object.__setattr__(self, "created_at", dt_util.as_utc(self.created_at))
        object.__setattr__(self, "items", items)

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable active restore work."""
        return {
            "episode_id": self.episode_id,
            "presence_entity": self.presence_entity,
            "created_at": self.created_at.isoformat(),
            "day_type_at_shutdown": self.day_type_at_shutdown.value,
            "items": [item.as_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self | None:
        """Restore a plan, failing closed if any contained item is malformed."""
        if not isinstance(data, Mapping):
            return None
        episode_id = _optional_string(data.get("episode_id"))
        presence_entity = _optional_string(data.get("presence_entity"))
        created_at = _parse_datetime(data.get("created_at"))
        raw_items = data.get("items")
        if (
            episode_id is None
            or presence_entity is None
            or created_at is None
            or not isinstance(raw_items, Sequence)
            or isinstance(raw_items, (str, bytes, bytearray))
        ):
            return None
        try:
            day_type = DayType(data.get("day_type_at_shutdown"))
        except (TypeError, ValueError):
            return None
        items: list[RestoreItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                return None
            item = RestoreItem.from_dict(raw_item)
            if item is None:
                return None
            items.append(item)
        try:
            return cls(
                episode_id=episode_id,
                presence_entity=presence_entity,
                created_at=created_at,
                day_type_at_shutdown=day_type,
                items=tuple(items),
            )
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class LastRestoration:
    """Persisted outcome of the latest presence-triggered restoration."""

    episode_id: str
    occurred_at: datetime
    restored_entities: tuple[str, ...] = ()
    skipped_entities: Mapping[str, str] = field(default_factory=dict)
    failed_entities: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Copy outcome collections and reject contradictory results."""
        episode_id = _optional_string(self.episode_id)
        restored = _strict_string_sequence(self.restored_entities)
        skipped = _strict_string_mapping(self.skipped_entities)
        failed = _strict_string_mapping(self.failed_entities)
        if episode_id is None:
            raise ValueError("Restoration episode identity is required")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
        ):
            raise ValueError("Restoration time must be timezone-aware")
        if restored is None or skipped is None or failed is None:
            raise ValueError("Restoration outcomes are malformed")
        restored_set = set(restored)
        if (
            restored_set & skipped.keys()
            or restored_set & failed.keys()
            or skipped.keys() & failed.keys()
        ):
            raise ValueError("An entity cannot have multiple restoration outcomes")
        object.__setattr__(self, "episode_id", episode_id)
        object.__setattr__(self, "occurred_at", dt_util.as_utc(self.occurred_at))
        object.__setattr__(self, "restored_entities", restored)
        object.__setattr__(self, "skipped_entities", skipped)
        object.__setattr__(self, "failed_entities", failed)

    @property
    def partial_failure(self) -> bool:
        """Return whether some restored entities accompanied a hard failure."""
        return bool(self.restored_entities and self.failed_entities)

    @property
    def succeeded(self) -> bool:
        """Return whether no restoration attempt had a hard failure."""
        return not self.failed_entities

    @property
    def timestamp(self) -> datetime:
        """Return the restoration timestamp."""
        return self.occurred_at

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable restoration outcomes."""
        return {
            "episode_id": self.episode_id,
            "occurred_at": self.occurred_at.isoformat(),
            "restored_entities": list(self.restored_entities),
            "skipped_entities": dict(self.skipped_entities),
            "failed_entities": dict(self.failed_entities),
            "partial_failure": self.partial_failure,
            "succeeded": self.succeeded,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self | None:
        """Restore outcomes, returning None for malformed or overlapping data."""
        if not isinstance(data, Mapping):
            return None
        episode_id = _optional_string(data.get("episode_id"))
        occurred_at = _parse_datetime(data.get("occurred_at"))
        restored = _strict_string_sequence(data.get("restored_entities", []))
        skipped = _strict_string_mapping(data.get("skipped_entities", {}))
        failed = _strict_string_mapping(data.get("failed_entities", {}))
        if (
            episode_id is None
            or occurred_at is None
            or restored is None
            or skipped is None
            or failed is None
        ):
            return None
        try:
            return cls(
                episode_id=episode_id,
                occurred_at=occurred_at,
                restored_entities=restored,
                skipped_entities=skipped,
                failed_entities=failed,
            )
        except ValueError:
            return None


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
RestorePhase = RestoreItemPhase
