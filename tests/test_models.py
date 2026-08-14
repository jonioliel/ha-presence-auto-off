"""Serialization tests for Presence Auto-Off models."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

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
    DEFAULT_DELAY_SECONDS,
)
from custom_components.presence_auto_off.models import (
    AbsenceEpisode,
    ActivityEvent,
    ActivityEventType,
    DayType,
    EntityStateSnapshot,
    LastExecution,
    LastRestoration,
    RestoreItem,
    RestoreItemPhase,
    RestorePlan,
    RuleConfig,
    Status,
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
            CONF_RESTORE_ON_PRESENCE: True,
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
    assert config.restore_on_presence
    assert config.rule_id == "rule-id"

    serialized = config.as_dict()
    json.dumps(serialized)
    assert RuleConfig.from_mapping(serialized) == config


@pytest.mark.parametrize("raw_value", ("true", 1, None, [], {}))
def test_rule_config_restore_option_is_strict_boolean(raw_value: object) -> None:
    """Only a literal true enables automatic restoration."""
    config = RuleConfig.from_mapping(
        {
            CONF_PRESENCE_ENTITY: "binary_sensor.presence",
            CONF_RESTORE_ON_PRESENCE: raw_value,
        }
    )

    assert not config.restore_on_presence
    assert config.as_dict()[CONF_RESTORE_ON_PRESENCE] is False


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


def test_entity_state_snapshot_is_json_safe_and_detached() -> None:
    """Snapshot attributes are deeply copied on input, output, and restore."""
    attributes = {
        "brightness": 128,
        "settings": {"modes": ["heat", "eco"]},
        "coordinates": (1.5, 2.5),
    }
    captured_at = datetime(2026, 8, 14, 9, 10, tzinfo=UTC)
    snapshot = EntityStateSnapshot(
        state="on",
        attributes=attributes,
        captured_at=captured_at,
    )

    attributes["settings"]["modes"].append("manual")
    serialized = snapshot.as_dict()
    serialized["attributes"]["settings"]["modes"].append("away")

    assert snapshot.attributes == {
        "brightness": 128,
        "settings": {"modes": ["heat", "eco"]},
        "coordinates": [1.5, 2.5],
    }
    json.dumps(snapshot.as_dict())
    assert EntityStateSnapshot.from_dict(snapshot.as_dict()) == snapshot

    malformed = snapshot.as_dict()
    malformed["attributes"] = {"not_json": object()}
    assert EntityStateSnapshot.from_dict(malformed) is None


def test_restore_items_enforce_phase_invariants() -> None:
    """All durable phases round-trip while unknown or inconsistent data fails."""
    captured_at = datetime(2026, 8, 14, 9, 10, tzinfo=UTC)
    before = EntityStateSnapshot("on", {"brightness": 128}, captured_at)
    after_off = EntityStateSnapshot("off", {}, captured_at + timedelta(seconds=1))
    items = (
        RestoreItem(
            registry_entry_id="registry-prepared",
            entity_id_at_capture="light.prepared",
            before=before,
            phase=RestoreItemPhase.PREPARED,
        ),
        RestoreItem(
            registry_entry_id="registry-ready",
            entity_id_at_capture="light.ready",
            before=before,
            phase=RestoreItemPhase.READY,
            after_off=after_off,
            modified_since_off=True,
        ),
        RestoreItem(
            registry_entry_id="registry-restoring",
            entity_id_at_capture="light.restoring",
            before=before,
            phase=RestoreItemPhase.RESTORING,
            after_off=after_off,
        ),
    )

    for item in items:
        serialized = item.as_dict()
        json.dumps(serialized)
        assert RestoreItem.from_dict(serialized) == item
        assert item.registry_id == item.registry_entry_id

    malformed = items[1].as_dict()
    malformed["phase"] = "future_phase"
    assert RestoreItem.from_dict(malformed) is None

    malformed = items[1].as_dict()
    malformed["phase"] = RestoreItemPhase.PREPARED.value
    assert RestoreItem.from_dict(malformed) is None

    malformed = items[1].as_dict()
    malformed["modified_since_off"] = 1
    assert RestoreItem.from_dict(malformed) is None


def test_restore_plan_round_trip_and_fail_closed_validation() -> None:
    """A plan remains tied to one episode and rejects any malformed item."""
    captured_at = datetime(2026, 8, 14, 9, 10, tzinfo=UTC)
    item = RestoreItem(
        registry_entry_id="registry-light",
        entity_id_at_capture="light.room",
        before=EntityStateSnapshot("on", {"brightness": 128}, captured_at),
        phase=RestoreItemPhase.READY,
        after_off=EntityStateSnapshot(
            "off", {"brightness": 128}, captured_at + timedelta(seconds=1)
        ),
    )
    plan = RestorePlan(
        episode_id="episode-id",
        presence_entity="binary_sensor.presence",
        created_at=captured_at,
        day_type_at_shutdown=DayType.ORDINARY,
        items=(item,),
    )

    serialized = plan.as_dict()
    json.dumps(serialized)
    assert RestorePlan.from_dict(serialized) == plan

    malformed = plan.as_dict()
    malformed["items"][0]["phase"] = "unknown"
    assert RestorePlan.from_dict(malformed) is None

    malformed = plan.as_dict()
    malformed["presence_entity"] = " "
    assert RestorePlan.from_dict(malformed) is None

    with pytest.raises(ValueError, match="duplicate"):
        RestorePlan(
            episode_id="episode-id",
            presence_entity="binary_sensor.presence",
            created_at=captured_at,
            day_type_at_shutdown=DayType.ORDINARY,
            items=(item, item),
        )


def test_last_restoration_round_trip_and_outcome_validation() -> None:
    """Restore outcome mappings are copied and cannot overlap."""
    skipped = {"light.two": "modified_since_off"}
    restoration = LastRestoration(
        episode_id="episode-id",
        occurred_at=datetime(2026, 8, 14, 9, 15, tzinfo=UTC),
        restored_entities=("light.one",),
        skipped_entities=skipped,
        failed_entities={"climate.three": "service_timeout"},
    )
    skipped["switch.late"] = "late mutation"

    assert restoration.partial_failure
    assert not restoration.succeeded
    assert "switch.late" not in restoration.skipped_entities
    serialized = restoration.as_dict()
    json.dumps(serialized)
    assert LastRestoration.from_dict(serialized) == restoration

    malformed = restoration.as_dict()
    malformed["failed_entities"] = {"light.one": "duplicate outcome"}
    assert LastRestoration.from_dict(malformed) is None


def test_restore_enum_values_are_stable() -> None:
    """New status and activity values remain suitable for persisted/UI state."""
    assert Status.RESTORING.value == "restoring"
    assert ActivityEventType.RESTORED.value == "restored"
    assert ActivityEventType.RESTORE_SKIPPED.value == "restore_skipped"
    assert ActivityEventType.RESTORE_FAILED.value == "restore_failed"


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
