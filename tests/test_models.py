"""Serialization tests for Presence Auto-Off models."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from custom_components.presence_auto_off.const import (
    CONF_ALLOWED_DAY_TYPES,
    CONF_AREA_ID,
    CONF_DELAY_SECONDS,
    CONF_HOLIDAY_ENTITY,
    CONF_NAME,
    CONF_PRESENCE_ENTITY,
    CONF_RULE_ID,
    CONF_SHABBAT_ENTITY,
    CONF_TARGET_ENTITIES,
    DEFAULT_DELAY_SECONDS,
)
from custom_components.presence_auto_off.models import (
    AbsenceEpisode,
    ActivityEvent,
    ActivityEventType,
    DayType,
    LastExecution,
    RuleConfig,
)


def test_rule_config_normalizes_and_round_trips() -> None:
    """Rule configuration is normalized and remains JSON serializable."""
    config = RuleConfig.from_mapping(
        {
            CONF_NAME: "  Kitchen  ",
            CONF_AREA_ID: " kitchen ",
            CONF_PRESENCE_ENTITY: " binary_sensor.kitchen_presence ",
            CONF_DELAY_SECONDS: "12.5",
            CONF_TARGET_ENTITIES: [
                " light.kitchen ",
                "light.kitchen",
                "switch.floor_heat",
            ],
            CONF_SHABBAT_ENTITY: "binary_sensor.shabbat",
            CONF_HOLIDAY_ENTITY: " ",
            CONF_ALLOWED_DAY_TYPES: [
                DayType.ORDINARY.value,
                DayType.UNKNOWN.value,
                "invalid",
                DayType.ORDINARY.value,
            ],
            CONF_RULE_ID: " rule-id ",
        }
    )

    assert config.name == "Kitchen"
    assert config.area_id == "kitchen"
    assert config.presence_entity == "binary_sensor.kitchen_presence"
    assert config.delay_seconds == 12.5
    assert config.target_entities == (
        "light.kitchen",
        "switch.floor_heat",
    )
    assert config.shabbat_entity == "binary_sensor.shabbat"
    assert config.holiday_entity is None
    assert config.allowed_day_types == frozenset({DayType.ORDINARY})
    assert config.rule_id == "rule-id"

    serialized = config.as_dict()
    json.dumps(serialized)
    assert RuleConfig.from_mapping(serialized) == config


def test_rule_config_replaces_non_finite_delay() -> None:
    """A non-finite duration cannot enter persisted controller state."""
    config = RuleConfig.from_mapping(
        {
            CONF_PRESENCE_ENTITY: "binary_sensor.presence",
            CONF_DELAY_SECONDS: float("nan"),
        }
    )

    assert config.delay_seconds == float(DEFAULT_DELAY_SECONDS)


def test_absence_episode_round_trip_and_validation() -> None:
    """Absence episodes preserve aware UTC times and reject bad storage."""
    started_at = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
    episode = AbsenceEpisode(
        episode_id="episode-id",
        generation=7,
        presence_entity="binary_sensor.presence",
        started_at=started_at,
        deadline=started_at + timedelta(minutes=10),
        completed=True,
        completed_at=started_at + timedelta(minutes=11),
    )

    serialized = episode.as_dict()
    json.dumps(serialized)
    assert AbsenceEpisode.from_dict(serialized) == episode

    malformed = dict(serialized)
    malformed["deadline"] = "2026-08-14T09:10:00"
    assert AbsenceEpisode.from_dict(malformed) is None

    malformed = dict(serialized)
    malformed["generation"] = True
    assert AbsenceEpisode.from_dict(malformed) is None


def test_last_execution_round_trip_and_result_flags() -> None:
    """Execution results preserve partial failure details."""
    execution = LastExecution(
        episode_id="episode-id",
        occurred_at=datetime(2026, 8, 14, 9, 10, tzinfo=UTC),
        successful_entities=("light.one",),
        failed_entities={"switch.two": "HomeAssistantError: failed"},
    )

    assert execution.partial_failure
    assert not execution.succeeded
    serialized = execution.as_dict()
    json.dumps(serialized)
    assert LastExecution.from_dict(serialized) == execution
    assert LastExecution.from_dict({"episode_id": "missing-time"}) is None


def test_activity_event_serializes() -> None:
    """Activity events produce a stable JSON-safe payload."""
    activity = ActivityEvent(
        event_type=ActivityEventType.BLOCKED,
        occurred_at=datetime(2026, 8, 14, 9, 10, tzinfo=UTC),
        data={"reason": "day_type_not_allowed", "targets": 2},
    )

    serialized = activity.as_dict()
    json.dumps(serialized)
    assert serialized == {
        "event_type": "blocked",
        "occurred_at": "2026-08-14T09:10:00+00:00",
        "data": {"reason": "day_type_not_allowed", "targets": 2},
    }
