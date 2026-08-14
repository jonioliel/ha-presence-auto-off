"""Tests for the event-driven Presence Auto-Off controller."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import split_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from custom_components.presence_auto_off.controller import (
    PresenceAutoOffController,
)
from custom_components.presence_auto_off.models import (
    ALL_DAY_TYPES,
    ActivityEventType,
    DayType,
    RuleConfig,
    Status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall


PRESENCE_ENTITY = "binary_sensor.room_presence"
SHABBAT_ENTITY = "binary_sensor.shabbat"
HOLIDAY_ENTITY = "binary_sensor.holiday"
DEFAULT_TARGET = "light.room"


@dataclass(slots=True)
class TurnOffRecorder:
    """Record target service calls and optionally fail selected entities."""

    calls: list[str] = field(default_factory=list)
    failing_entities: set[str] = field(default_factory=set)

    async def async_handle(self, call: ServiceCall) -> None:
        """Handle a registered turn-off service call."""
        entity_id = call.data[ATTR_ENTITY_ID]
        assert isinstance(entity_id, str)
        self.calls.append(entity_id)
        if entity_id in self.failing_entities:
            raise HomeAssistantError("simulated failure")


type ControllerFactory = Callable[
    [RuleConfig, str | None], Awaitable[PresenceAutoOffController]
]


@pytest.fixture
def turn_off_recorder(hass: HomeAssistant) -> TurnOffRecorder:
    """Register controllable domains and return their call recorder."""
    recorder = TurnOffRecorder()
    for domain in ("climate", "light", "switch"):
        hass.services.async_register(domain, SERVICE_TURN_OFF, recorder.async_handle)
    return recorder


@pytest.fixture
async def controller_factory(
    hass: HomeAssistant,
) -> AsyncIterator[ControllerFactory]:
    """Create controllers and unload every one after a test."""
    controllers: list[PresenceAutoOffController] = []

    async def factory(
        config: RuleConfig, entry_id: str | None = None
    ) -> PresenceAutoOffController:
        area_registry = ar.async_get(hass)
        if (
            config.area_id is not None
            and area_registry.async_get_area(config.area_id) is None
        ):
            created_area = area_registry.async_create(config.area_id)
            assert created_area.id == config.area_id

        entity_registry = er.async_get(hass)
        for entity_id in config.target_entities:
            registry_entry = entity_registry.async_get(entity_id)
            if registry_entry is None:
                domain, object_id = split_entity_id(entity_id)
                registry_entry = entity_registry.async_get_or_create(
                    domain,
                    "test",
                    entity_id,
                    suggested_object_id=object_id,
                )
                assert registry_entry.entity_id == entity_id
            entity_registry.async_update_entity(
                entity_id,
                area_id=config.area_id,
            )
            if hass.states.get(entity_id) is None:
                hass.states.async_set(entity_id, STATE_ON)

        controller = PresenceAutoOffController(
            hass,
            entry_id or f"test-entry-{len(controllers) + 1}",
            config,
        )
        controllers.append(controller)
        await controller.async_setup()
        return controller

    yield factory

    for controller in reversed(controllers):
        await controller.async_unload()


def _rule_config(
    *,
    area_id: str = "test_room",
    delay_seconds: float = 0,
    target_entities: tuple[str, ...] = (DEFAULT_TARGET,),
    shabbat_entity: str | None = None,
    holiday_entity: str | None = None,
    allowed_day_types: frozenset[DayType] = ALL_DAY_TYPES,
) -> RuleConfig:
    """Build a compact controller configuration for tests."""
    return RuleConfig(
        name="Test room",
        area_id=area_id,
        presence_entity=PRESENCE_ENTITY,
        delay_seconds=delay_seconds,
        target_entities=target_entities,
        shabbat_entity=shabbat_entity,
        holiday_entity=holiday_entity,
        allowed_day_types=allowed_day_types,
        rule_id="test-rule",
    )


async def test_setup_save_failure_removes_all_live_callbacks(
    hass: HomeAssistant,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """A failed initial save must leave no listener, timer, or stale action."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    controller = PresenceAutoOffController(
        hass,
        "save-failure-entry",
        _rule_config(delay_seconds=3600),
    )
    remove_state_listener = Mock()
    remove_deadline = Mock()

    with (
        patch(
            "custom_components.presence_auto_off.controller."
            "async_track_state_change_event",
            return_value=remove_state_listener,
        ),
        patch(
            "custom_components.presence_auto_off.controller."
            "async_track_point_in_utc_time",
            return_value=remove_deadline,
        ) as track_deadline,
        patch.object(
            controller._store,
            "async_save",
            new=AsyncMock(side_effect=OSError("storage unavailable")),
        ),
        pytest.raises(OSError, match="storage unavailable"),
    ):
        await controller.async_setup()

    scheduled_callback = track_deadline.call_args.args[1]
    scheduled_at = track_deadline.call_args.args[2]
    state_reference_cleared = controller._unsub_state is None
    deadline_reference_cleared = controller._unsub_deadline is None
    state_listener_removed = remove_state_listener.call_count == 1
    deadline_removed = remove_deadline.call_count == 1

    try:
        # Exercise a deadline callback that was already queued when storage
        # failed. Cleanup must make this stale callback harmless.
        await scheduled_callback(scheduled_at)
        no_stale_execution = (
            turn_off_recorder.calls == [] and controller.last_execution is None
        )
    finally:
        await controller.async_unload()

    assert state_reference_cleared
    assert deadline_reference_cleared
    assert state_listener_removed
    assert deadline_removed
    assert no_stale_execution


@pytest.mark.parametrize("operation_name", ("async_stop", "async_unload"))
async def test_stop_is_safety_first_when_persistence_fails(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    operation_name: str,
) -> None:
    """Stopping remains successful after listeners are removed if save fails."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    controller = await controller_factory(_rule_config(delay_seconds=3600))
    assert controller._unsub_state is not None
    assert controller._unsub_deadline is not None

    failed_save = AsyncMock(side_effect=OSError("storage unavailable"))
    with patch.object(controller._store, "async_save", new=failed_save):
        operation = getattr(controller, operation_name)
        await operation()

    failed_save.assert_awaited_once()
    assert controller._unloaded
    assert controller._unsub_state is None
    assert controller._unsub_deadline is None


async def test_immediate_absence_executes_once(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """An already empty room with a zero delay is shut down immediately."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)

    controller = await controller_factory(_rule_config())

    assert turn_off_recorder.calls == [DEFAULT_TARGET]
    assert controller.status is Status.COMPLETED
    assert controller.deadline is None
    assert controller.last_execution is not None
    assert controller.last_execution.successful_entities == (DEFAULT_TARGET,)
    assert controller.last_activity is not None
    assert controller.last_activity.event_type is ActivityEventType.EXECUTED


async def test_presence_return_cancels_pending_shutdown(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """Presence returning before the deadline invalidates the episode."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(delay_seconds=3600))

    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.status is Status.COUNTDOWN
    assert controller.deadline is not None

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    assert controller.status is Status.OCCUPIED
    assert controller.deadline is None
    assert controller.episode_id is None
    assert turn_off_recorder.calls == []
    assert controller.last_activity is not None
    assert controller.last_activity.event_type is ActivityEventType.NO_ACTION
    assert controller.last_activity.data["reason"] == "presence_returned"


async def test_unavailable_presence_requires_a_fresh_off_state(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """An unusable presence state cancels the episode until a fresh OFF."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    hass.states.async_set(SHABBAT_ENTITY, STATE_ON)
    controller = await controller_factory(
        _rule_config(
            shabbat_entity=SHABBAT_ENTITY,
            allowed_day_types=frozenset({DayType.ORDINARY}),
        )
    )
    blocked_episode_id = controller.episode_id

    assert controller.status is Status.WAITING_CONDITION
    assert blocked_episode_id is not None
    assert turn_off_recorder.calls == []

    hass.states.async_set(PRESENCE_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert controller.status is Status.SENSOR_UNAVAILABLE
    assert controller.episode_id is None
    assert controller.deadline is None
    assert turn_off_recorder.calls == []

    hass.states.async_set(SHABBAT_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.status is Status.SENSOR_UNAVAILABLE
    assert controller.episode_id is None

    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    assert controller.status is Status.COMPLETED
    assert controller.episode_id is not None
    assert controller.episode_id != blocked_episode_id
    assert turn_off_recorder.calls == [DEFAULT_TARGET]


@pytest.mark.parametrize(
    (
        "allowed_day_types",
        "initial_shabbat",
        "initial_holiday",
        "changed_entity",
        "changed_state",
        "blocked_day_type",
        "allowed_day_type",
    ),
    [
        (
            frozenset({DayType.ORDINARY}),
            STATE_ON,
            STATE_OFF,
            SHABBAT_ENTITY,
            STATE_OFF,
            DayType.SHABBAT,
            DayType.ORDINARY,
        ),
        (
            frozenset({DayType.SHABBAT}),
            STATE_OFF,
            STATE_OFF,
            SHABBAT_ENTITY,
            STATE_ON,
            DayType.ORDINARY,
            DayType.SHABBAT,
        ),
        (
            frozenset({DayType.HOLIDAY}),
            STATE_ON,
            STATE_OFF,
            HOLIDAY_ENTITY,
            STATE_ON,
            DayType.SHABBAT,
            DayType.HOLIDAY,
        ),
    ],
    ids=("ordinary", "shabbat", "holiday"),
)
async def test_day_gate_blocks_then_executes_when_allowed(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    allowed_day_types: frozenset[DayType],
    initial_shabbat: str,
    initial_holiday: str,
    changed_entity: str,
    changed_state: str,
    blocked_day_type: DayType,
    allowed_day_type: DayType,
) -> None:
    """Every day profile waits while blocked and executes when allowed."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    hass.states.async_set(SHABBAT_ENTITY, initial_shabbat)
    hass.states.async_set(HOLIDAY_ENTITY, initial_holiday)
    controller = await controller_factory(
        _rule_config(
            shabbat_entity=SHABBAT_ENTITY,
            holiday_entity=HOLIDAY_ENTITY,
            allowed_day_types=allowed_day_types,
        )
    )

    assert controller.day_type is blocked_day_type
    assert controller.status is Status.WAITING_CONDITION
    assert not controller.gate_allowed
    assert turn_off_recorder.calls == []
    assert controller.last_activity is not None
    assert controller.last_activity.event_type is ActivityEventType.BLOCKED

    hass.states.async_set(changed_entity, changed_state)
    await hass.async_block_till_done()

    assert controller.day_type is allowed_day_type
    assert controller.gate_allowed
    assert controller.status is Status.COMPLETED
    assert turn_off_recorder.calls == [DEFAULT_TARGET]


async def test_unavailable_gate_fails_closed_except_for_all_day_rule(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """Unknown gate data blocks restricted rules but not an all-day rule."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    hass.states.async_set(SHABBAT_ENTITY, STATE_UNAVAILABLE)

    restricted = await controller_factory(
        _rule_config(
            shabbat_entity=SHABBAT_ENTITY,
            allowed_day_types=frozenset({DayType.ORDINARY}),
        ),
        "restricted-entry",
    )

    assert restricted.day_type is DayType.UNKNOWN
    assert not restricted.gate_allowed
    assert restricted.status is Status.SENSOR_UNAVAILABLE
    assert turn_off_recorder.calls == []
    assert restricted.last_activity is not None
    assert restricted.last_activity.data["reason"] == "gate_sensor_unavailable"

    await restricted.async_unload()
    all_day = await controller_factory(
        _rule_config(
            shabbat_entity=SHABBAT_ENTITY,
            allowed_day_types=ALL_DAY_TYPES,
        ),
        "all-day-entry",
    )

    assert all_day.day_type is DayType.UNKNOWN
    assert all_day.gate_allowed
    assert all_day.status is Status.COMPLETED
    assert turn_off_recorder.calls == [DEFAULT_TARGET]


async def test_target_failures_are_isolated(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """A failing entity does not prevent later targets from being attempted."""
    targets = ("light.first", "switch.failure", "climate.last")
    turn_off_recorder.failing_entities.add("switch.failure")
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)

    controller = await controller_factory(_rule_config(target_entities=targets))

    assert turn_off_recorder.calls == list(targets)
    assert controller.status is Status.ERROR
    assert controller.last_execution is not None
    assert controller.last_execution.successful_entities == (
        "light.first",
        "climate.last",
    )
    assert set(controller.last_execution.failed_entities) == {"switch.failure"}
    assert controller.last_execution.partial_failure
    assert not controller.last_execution.succeeded
    assert controller.last_activity is not None
    assert controller.last_activity.event_type is ActivityEventType.FAILED


async def test_turn_off_timeout_is_recorded_and_does_not_block_unload(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
) -> None:
    """A service that never returns is bounded while unload waits for it."""
    service_started = asyncio.Event()
    release_service = asyncio.Event()

    async def async_never_return(_call: ServiceCall) -> None:
        service_started.set()
        await release_service.wait()

    hass.services.async_register("light", SERVICE_TURN_OFF, async_never_return)
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config())

    with patch(
        "custom_components.presence_auto_off.controller."
        "_TARGET_SERVICE_TIMEOUT_SECONDS",
        0.01,
    ):
        hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
        await asyncio.wait_for(service_started.wait(), timeout=1)
        try:
            await asyncio.wait_for(controller.async_unload(), timeout=1)
        finally:
            # Release a service task if the test fails before timeout cleanup.
            release_service.set()

    await hass.async_block_till_done()

    assert controller.last_execution is not None
    assert controller.last_execution.successful_entities == ()
    assert controller.last_execution.failed_entities == {
        DEFAULT_TARGET: "service_timeout"
    }
    assert controller.last_activity is not None
    assert controller.last_activity.event_type is ActivityEventType.FAILED


async def test_target_moved_out_of_configured_area_is_not_called(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """Runtime area drift is recorded as failure without touching the target."""
    area_registry = ar.async_get(hass)
    configured_area = area_registry.async_create("Configured room")
    other_area = area_registry.async_create("Other room")
    entity_registry = er.async_get(hass)
    target = entity_registry.async_get_or_create(
        "light",
        "test",
        "movable-light",
        suggested_object_id="movable_light",
    )
    target = entity_registry.async_update_entity(
        target.entity_id, area_id=configured_area.id
    )

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(
        _rule_config(
            area_id=configured_area.id,
            target_entities=(target.entity_id,),
        )
    )

    entity_registry.async_update_entity(target.entity_id, area_id=other_area.id)
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    assert turn_off_recorder.calls == []
    assert controller.status is Status.ERROR
    assert controller.last_execution is not None
    assert controller.last_execution.successful_entities == ()
    assert target.entity_id in controller.last_execution.failed_entities
    assert controller.last_activity is not None
    assert controller.last_activity.event_type is ActivityEventType.FAILED


async def test_completed_episode_is_not_repeated_after_restart(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """A persisted completed episode remains at-most-once after restart."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    config = _rule_config()
    first = await controller_factory(config, "restart-entry")
    first_episode_id = first.episode_id

    assert first.status is Status.COMPLETED
    assert turn_off_recorder.calls == [DEFAULT_TARGET]
    await first.async_unload()

    restarted = await controller_factory(config, "restart-entry")

    assert restarted.status is Status.COMPLETED
    assert restarted.episode_id == first_episode_id
    assert restarted.last_execution is not None
    assert restarted.last_execution.episode_id == first_episode_id
    assert turn_off_recorder.calls == [DEFAULT_TARGET]


async def test_pending_deadline_survives_reload_and_uses_new_delay(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """Reloads retain absence start and derive deadlines from current options."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    initial_config = _rule_config(delay_seconds=3600)
    first = await controller_factory(initial_config, "pending-entry")
    started_at = first.absence_started_at
    deadline = first.deadline
    episode_id = first.episode_id

    assert first.status is Status.COUNTDOWN
    assert started_at is not None
    assert deadline is not None
    assert (deadline - started_at).total_seconds() == 3600
    await first.async_unload()

    same_delay = await controller_factory(initial_config, "pending-entry")
    assert same_delay.status is Status.COUNTDOWN
    assert same_delay.episode_id == episode_id
    assert same_delay.absence_started_at == started_at
    assert same_delay.deadline == deadline
    await same_delay.async_unload()

    longer_delay = await controller_factory(
        _rule_config(delay_seconds=7200), "pending-entry"
    )
    assert longer_delay.status is Status.COUNTDOWN
    assert longer_delay.episode_id == episode_id
    assert longer_delay.absence_started_at == started_at
    assert longer_delay.deadline is not None
    assert (longer_delay.deadline - started_at).total_seconds() == 7200
    assert turn_off_recorder.calls == []


async def test_enabled_state_persists_and_reenable_reconciles_absence(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """The enable switch persists and starts a fresh check when turned on."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    config = _rule_config()
    controller = await controller_factory(config, "enabled-entry")

    await controller.async_set_enabled(False)
    assert not controller.enabled
    assert controller.status is Status.DISABLED

    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.status is Status.DISABLED
    assert turn_off_recorder.calls == []
    await controller.async_unload()

    restarted = await controller_factory(config, "enabled-entry")
    assert not restarted.enabled
    assert restarted.status is Status.DISABLED
    assert turn_off_recorder.calls == []

    await restarted.async_set_enabled(True)
    assert restarted.enabled
    assert restarted.status is Status.COMPLETED
    assert turn_off_recorder.calls == [DEFAULT_TARGET]

    await restarted.async_set_enabled(True)
    assert turn_off_recorder.calls == [DEFAULT_TARGET]
