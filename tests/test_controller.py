"""Tests for the event-driven Presence Auto-Off controller."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
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
from homeassistant.core import State, split_entity_id
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
    RestoreItemPhase,
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

    hass: HomeAssistant
    calls: list[str] = field(default_factory=list)
    failing_entities: set[str] = field(default_factory=set)

    async def async_handle(self, call: ServiceCall) -> None:
        """Handle a registered turn-off service call."""
        entity_id = call.data[ATTR_ENTITY_ID]
        assert isinstance(entity_id, str)
        self.calls.append(entity_id)
        if entity_id in self.failing_entities:
            raise HomeAssistantError("simulated failure")
        self.hass.states.async_set(entity_id, STATE_OFF)


@dataclass(slots=True)
class RestoreRecorder:
    """Reproduce captured states while retaining exact requested snapshots."""

    calls: list[State] = field(default_factory=list)
    failing_entities: set[str] = field(default_factory=set)

    async def async_reproduce(self, hass: HomeAssistant, state: State) -> None:
        """Apply one desired state like HA's domain reproducer."""
        self.calls.append(state)
        if state.entity_id in self.failing_entities:
            raise HomeAssistantError("simulated restore failure")
        hass.states.async_set(state.entity_id, state.state, dict(state.attributes))


type ControllerFactory = Callable[
    [RuleConfig, str | None], Awaitable[PresenceAutoOffController]
]


@pytest.fixture
def turn_off_recorder(hass: HomeAssistant) -> TurnOffRecorder:
    """Register controllable domains and return their call recorder."""
    recorder = TurnOffRecorder(hass)
    for domain in ("climate", "light", "switch"):
        hass.services.async_register(domain, SERVICE_TURN_OFF, recorder.async_handle)
    return recorder


@pytest.fixture
def restore_recorder(monkeypatch: pytest.MonkeyPatch) -> RestoreRecorder:
    """Replace HA state reproduction with a deterministic recorder."""
    recorder = RestoreRecorder()
    monkeypatch.setattr(
        "custom_components.presence_auto_off.controller.async_reproduce_state",
        recorder.async_reproduce,
    )
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
        for entity_id in (
            config.presence_entity,
            config.shabbat_entity,
            config.holiday_entity,
        ):
            if entity_id is None or entity_registry.async_get(entity_id) is not None:
                continue
            domain, object_id = split_entity_id(entity_id)
            registry_entry = entity_registry.async_get_or_create(
                domain,
                "test",
                f"configured-input-{entity_id}",
                suggested_object_id=object_id,
            )
            assert registry_entry.entity_id == entity_id

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
    presence_entity: str = PRESENCE_ENTITY,
    delay_seconds: float = 0,
    target_entities: tuple[str, ...] = (DEFAULT_TARGET,),
    shabbat_entity: str | None = None,
    holiday_entity: str | None = None,
    allowed_day_types: frozenset[DayType] = ALL_DAY_TYPES,
    restore_on_presence: bool = False,
) -> RuleConfig:
    """Build a compact controller configuration for tests."""
    return RuleConfig(
        name="Test room",
        area_id=area_id,
        presence_entity=presence_entity,
        delay_seconds=delay_seconds,
        target_entities=target_entities,
        shabbat_entity=shabbat_entity,
        holiday_entity=holiday_entity,
        allowed_day_types=allowed_day_types,
        rule_id="test-rule",
        restore_on_presence=restore_on_presence,
    )


async def test_setup_save_failure_removes_all_live_callbacks(
    hass: HomeAssistant,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """A failed initial save must leave no listener, timer, or stale action."""
    presence_entry = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "test",
        "direct-setup-presence",
        suggested_object_id="room_presence",
    )
    assert presence_entry.entity_id == PRESENCE_ENTITY
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


async def test_restore_reproduces_only_selected_state_with_attributes(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """Presence restores the explicit target and leaves other entities untouched."""
    other = "light.not_selected"
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    hass.states.async_set(
        DEFAULT_TARGET, STATE_ON, {"brightness": 137, "effect": "none"}
    )
    hass.states.async_set(other, STATE_ON, {"brightness": 23})
    controller = await controller_factory(_rule_config(restore_on_presence=True))

    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    assert turn_off_recorder.calls == [DEFAULT_TARGET]
    assert controller.restore_plan is not None
    assert controller.restore_plan.items[0].phase is RestoreItemPhase.READY
    other_state = hass.states.get(other)
    assert other_state is not None
    assert other_state.state == STATE_ON

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    assert [state.entity_id for state in restore_recorder.calls] == [DEFAULT_TARGET]
    assert restore_recorder.calls[0].state == STATE_ON
    assert restore_recorder.calls[0].attributes["brightness"] == 137
    restored_state = hass.states.get(DEFAULT_TARGET)
    other_state = hass.states.get(other)
    assert restored_state is not None
    assert other_state is not None
    assert restored_state.attributes["brightness"] == 137
    assert other_state.attributes["brightness"] == 23
    assert controller.restore_plan is None
    assert controller.last_restoration is not None
    assert controller.last_restoration.restored_entities == (DEFAULT_TARGET,)
    assert controller.last_activity is not None
    assert controller.last_activity.event_type is ActivityEventType.RESTORED


async def test_already_off_target_is_never_restore_eligible(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """A target the controller did not change is not restored later."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    hass.states.async_set(DEFAULT_TARGET, STATE_OFF)
    controller = await controller_factory(_rule_config(restore_on_presence=True))

    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    assert turn_off_recorder.calls == [DEFAULT_TARGET]
    assert restore_recorder.calls == []
    assert controller.restore_plan is None


async def test_unconfirmed_turn_off_is_not_restore_eligible(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """A service return without an observed OFF state consumes PREPARED safely."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))

    async def async_no_state_change(call: ServiceCall) -> None:
        turn_off_recorder.calls.append(call.data[ATTR_ENTITY_ID])

    hass.services.async_register("light", SERVICE_TURN_OFF, async_no_state_change)
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    assert controller.restore_plan is None
    assert controller.last_execution is not None
    assert controller.last_execution.failed_entities == {
        DEFAULT_TARGET: "turn_off_not_confirmed"
    }

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()
    assert restore_recorder.calls == []


async def test_manual_primary_state_change_prevents_restore(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """A user toggle after shutdown is never overwritten on presence return."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))

    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None

    hass.states.async_set(DEFAULT_TARGET, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(DEFAULT_TARGET, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None
    assert controller.restore_plan.items[0].modified_since_off

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    assert restore_recorder.calls == []
    assert controller.last_restoration is not None
    assert controller.last_restoration.skipped_entities == {
        DEFAULT_TARGET: "modified_since_shutdown"
    }


@pytest.mark.parametrize(
    "manual_change",
    ("attributes", "unavailable_recovery", "removal_reappearance"),
)
async def test_any_post_shutdown_change_prevents_restore(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
    manual_change: str,
) -> None:
    """Attributes and availability cycles preserve post-shutdown intent."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None

    if manual_change == "attributes":
        hass.states.async_set(DEFAULT_TARGET, STATE_OFF, {"temperature": 19})
    elif manual_change == "unavailable_recovery":
        hass.states.async_set(DEFAULT_TARGET, STATE_UNAVAILABLE)
        await hass.async_block_till_done()
        hass.states.async_set(DEFAULT_TARGET, STATE_OFF)
    else:
        hass.states.async_remove(DEFAULT_TARGET)
        await hass.async_block_till_done()
        hass.states.async_set(DEFAULT_TARGET, STATE_OFF)
    await hass.async_block_till_done()

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    assert restore_recorder.calls == []
    assert controller.last_restoration is not None
    assert controller.last_restoration.skipped_entities == {
        DEFAULT_TARGET: "modified_since_shutdown"
    }


@pytest.mark.parametrize("unsafe_change", ("unavailable", "out_of_area"))
async def test_restore_revalidates_availability_and_area(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
    unsafe_change: str,
) -> None:
    """Runtime drift consumes restoration without touching an unsafe target."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None

    if unsafe_change == "unavailable":
        hass.states.async_set(DEFAULT_TARGET, STATE_UNAVAILABLE)
    else:
        other_area = ar.async_get(hass).async_create("Restore other room")
        er.async_get(hass).async_update_entity(DEFAULT_TARGET, area_id=other_area.id)
    await hass.async_block_till_done()

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    assert restore_recorder.calls == []
    assert controller.restore_plan is None
    assert controller.last_restoration is not None
    expected_reason = "unavailable" if unsafe_change == "unavailable" else "out_of_area"
    assert controller.last_restoration.skipped_entities == {
        DEFAULT_TARGET: expected_reason
    }


async def test_ready_restore_survives_restart_and_runs_once(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """READY persists while absent and is consumed once when presence returns."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    config = _rule_config(restore_on_presence=True)
    first = await controller_factory(config, "restore-restart-entry")
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    first_episode = first.restore_plan.episode_id if first.restore_plan else None
    assert first_episode is not None
    await first.async_unload()

    restarted = await controller_factory(config, "restore-restart-entry")
    assert restarted.restore_plan is not None
    assert restarted.restore_plan.episode_id == first_episode
    assert restore_recorder.calls == []

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()
    assert [state.entity_id for state in restore_recorder.calls] == [DEFAULT_TARGET]
    assert restarted.restore_plan is None
    await restarted.async_unload()

    final = await controller_factory(config, "restore-restart-entry")
    assert final.restore_plan is None
    assert [state.entity_id for state in restore_recorder.calls] == [DEFAULT_TARGET]


async def test_restore_gate_is_fail_closed_and_consumed_once(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """A blocked gate consumes READY so a later gate edge cannot revive it."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    hass.states.async_set(SHABBAT_ENTITY, STATE_OFF)
    controller = await controller_factory(
        _rule_config(
            shabbat_entity=SHABBAT_ENTITY,
            allowed_day_types=frozenset({DayType.ORDINARY}),
            restore_on_presence=True,
        )
    )
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None

    hass.states.async_set(SHABBAT_ENTITY, STATE_ON)
    await hass.async_block_till_done()
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    assert restore_recorder.calls == []
    assert controller.restore_plan is None
    assert controller.last_restoration is not None
    assert controller.last_restoration.skipped_entities == {
        DEFAULT_TARGET: "day_type_not_allowed"
    }

    hass.states.async_set(SHABBAT_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert restore_recorder.calls == []


async def test_presence_return_during_shutdown_restores_ready_success(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    restore_recorder: RestoreRecorder,
) -> None:
    """ON waits behind shutdown and restores the target confirmed OFF meanwhile."""
    service_started = asyncio.Event()
    release_service = asyncio.Event()

    async def async_delayed_off(call: ServiceCall) -> None:
        service_started.set()
        await release_service.wait()
        hass.states.async_set(call.data[ATTR_ENTITY_ID], STATE_OFF)

    hass.services.async_register("light", SERVICE_TURN_OFF, async_delayed_off)
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))

    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await asyncio.wait_for(service_started.wait(), timeout=1)
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    release_service.set()
    await hass.async_block_till_done()

    assert [state.entity_id for state in restore_recorder.calls] == [DEFAULT_TARGET]
    assert controller.restore_plan is None
    assert controller.status is Status.OCCUPIED


async def test_prepared_snapshot_save_failure_prevents_turn_off(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """Write-ahead failure fails closed before the external service call."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    original_save = controller._store.async_save

    async def async_fail_prepared(data: dict[str, object]) -> None:
        raw_plan = data.get("restore_plan")
        if isinstance(raw_plan, dict):
            items = raw_plan.get("items")
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("phase") == "prepared"
                for item in items
            ):
                raise OSError("prepared journal unavailable")
        await original_save(data)

    with patch.object(controller._store, "async_save", new=async_fail_prepared):
        hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
        await hass.async_block_till_done()

    assert turn_off_recorder.calls == []
    assert controller.restore_plan is None
    assert controller.last_execution is not None
    assert controller.last_execution.failed_entities == {
        DEFAULT_TARGET: "restore_snapshot_persist_failed"
    }


async def test_state_change_during_prepared_save_prevents_turn_off(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """A manual action during write-ahead invalidates the captured snapshot."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    original_save = controller._store.async_save
    prepared_save_started = asyncio.Event()
    release_prepared_save = asyncio.Event()

    async def async_delay_prepared(data: dict[str, object]) -> None:
        raw_plan = data.get("restore_plan")
        if isinstance(raw_plan, dict):
            items = raw_plan.get("items")
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("phase") == "prepared"
                for item in items
            ):
                prepared_save_started.set()
                await release_prepared_save.wait()
        await original_save(data)

    with patch.object(controller._store, "async_save", new=async_delay_prepared):
        hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
        await asyncio.wait_for(prepared_save_started.wait(), timeout=1)
        hass.states.async_set(DEFAULT_TARGET, STATE_OFF)
        release_prepared_save.set()
        await hass.async_block_till_done()

    assert turn_off_recorder.calls == []
    assert controller.restore_plan is None
    assert controller.last_execution is not None
    assert controller.last_execution.failed_entities == {
        DEFAULT_TARGET: "state_changed_before_shutdown"
    }


async def test_cycle_back_during_prepared_save_prevents_turn_off(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """ON→OFF→ON during write-ahead cannot hide behind equal final values."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    original_save = controller._store.async_save
    prepared_save_started = asyncio.Event()
    release_prepared_save = asyncio.Event()

    async def async_delay_prepared(data: dict[str, object]) -> None:
        raw_plan = data.get("restore_plan")
        if isinstance(raw_plan, dict):
            items = raw_plan.get("items")
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("phase") == "prepared"
                for item in items
            ):
                prepared_save_started.set()
                await release_prepared_save.wait()
        await original_save(data)

    with patch.object(controller._store, "async_save", new=async_delay_prepared):
        hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
        await asyncio.wait_for(prepared_save_started.wait(), timeout=1)
        hass.states.async_set(DEFAULT_TARGET, STATE_OFF)
        hass.states.async_set(DEFAULT_TARGET, STATE_ON)
        release_prepared_save.set()
        await hass.async_block_till_done()

    assert turn_off_recorder.calls == []
    assert controller.last_execution is not None
    assert controller.last_execution.failed_entities == {
        DEFAULT_TARGET: "state_changed_before_shutdown"
    }


async def test_presence_change_during_restoring_save_prevents_restore(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """RESTORING write-ahead is followed by a fresh presence safety check."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None

    original_save = controller._store.async_save
    restoring_save_started = asyncio.Event()
    release_restoring_save = asyncio.Event()
    delayed_once = False

    async def async_delay_restoring(data: dict[str, object]) -> None:
        nonlocal delayed_once
        raw_plan = data.get("restore_plan")
        if not delayed_once and isinstance(raw_plan, dict):
            items = raw_plan.get("items")
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("phase") == "restoring"
                for item in items
            ):
                delayed_once = True
                restoring_save_started.set()
                await release_restoring_save.wait()
        await original_save(data)

    with patch.object(controller._store, "async_save", new=async_delay_restoring):
        hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
        await asyncio.wait_for(restoring_save_started.wait(), timeout=1)
        hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
        release_restoring_save.set()
        await hass.async_block_till_done()

    assert restore_recorder.calls == []
    assert controller.last_restoration is not None
    assert controller.last_restoration.skipped_entities == {
        DEFAULT_TARGET: "presence_changed_during_restore"
    }


async def test_cycle_back_during_restoring_save_prevents_restore(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """OFF→ON→OFF during RESTORING claim is still a manual change."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None

    original_save = controller._store.async_save
    restoring_save_started = asyncio.Event()
    release_restoring_save = asyncio.Event()
    delayed_once = False

    async def async_delay_restoring(data: dict[str, object]) -> None:
        nonlocal delayed_once
        raw_plan = data.get("restore_plan")
        if not delayed_once and isinstance(raw_plan, dict):
            items = raw_plan.get("items")
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("phase") == "restoring"
                for item in items
            ):
                delayed_once = True
                restoring_save_started.set()
                await release_restoring_save.wait()
        await original_save(data)

    with patch.object(controller._store, "async_save", new=async_delay_restoring):
        hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
        await asyncio.wait_for(restoring_save_started.wait(), timeout=1)
        hass.states.async_set(DEFAULT_TARGET, STATE_ON)
        hass.states.async_set(DEFAULT_TARGET, STATE_OFF)
        release_restoring_save.set()
        await hass.async_block_till_done()

    assert restore_recorder.calls == []
    assert controller.last_restoration is not None
    assert controller.last_restoration.skipped_entities == {
        DEFAULT_TARGET: "state_changed_since_shutdown"
    }


async def test_presence_off_during_restore_aborts_remaining_and_restarts_absence(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new absence aborts remaining restores after the in-flight target."""
    second_target = "switch.room_fan"
    restore_calls: list[State] = []
    first_restore_started = asyncio.Event()
    release_first_restore = asyncio.Event()

    async def async_delayed_restore(hass: HomeAssistant, state: State) -> None:
        restore_calls.append(state)
        if len(restore_calls) == 1:
            first_restore_started.set()
            await release_first_restore.wait()
        hass.states.async_set(state.entity_id, state.state, dict(state.attributes))

    monkeypatch.setattr(
        "custom_components.presence_auto_off.controller.async_reproduce_state",
        async_delayed_restore,
    )
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(
        _rule_config(
            target_entities=(DEFAULT_TARGET, second_target),
            restore_on_presence=True,
        )
    )
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None
    restored_episode = controller.restore_plan.episode_id

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await asyncio.wait_for(first_restore_started.wait(), timeout=1)
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    release_first_restore.set()
    await hass.async_block_till_done()

    assert [state.entity_id for state in restore_calls] == [DEFAULT_TARGET]
    assert controller.last_restoration is not None
    assert controller.last_restoration.episode_id == restored_episode
    assert controller.last_restoration.restored_entities == (DEFAULT_TARGET,)
    assert controller.last_restoration.skipped_entities == {
        second_target: "presence_changed_during_restore"
    }
    assert controller.episode_id is not None
    assert controller.episode_id != restored_episode


async def test_presence_registry_rename_preserves_ready_restore(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """A READY plan follows the presence sensor's stable registry identity."""
    registry = er.async_get(hass)
    presence_entry = registry.async_get_or_create(
        "binary_sensor",
        "test",
        "presence-stable-id",
        suggested_object_id="room_presence",
    )
    assert presence_entry.entity_id == PRESENCE_ENTITY
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    config = _rule_config(restore_on_presence=True)
    first = await controller_factory(config, "presence-rename-entry")
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert first.restore_plan is not None
    assert first.restore_plan.presence_entity == presence_entry.id
    await first.async_unload()

    renamed_presence = "binary_sensor.renamed_room_presence"
    renamed_entry = registry.async_update_entity(
        PRESENCE_ENTITY,
        new_entity_id=renamed_presence,
    )
    assert renamed_entry.id == presence_entry.id
    hass.states.async_remove(PRESENCE_ENTITY)
    hass.states.async_set(renamed_presence, STATE_ON)

    restarted = await controller_factory(
        _rule_config(
            presence_entity=renamed_presence,
            restore_on_presence=True,
        ),
        "presence-rename-entry",
    )

    assert [state.entity_id for state in restore_recorder.calls] == [DEFAULT_TARGET]
    assert restarted.restore_plan is None
    assert restarted.last_restoration is not None
    assert restarted.last_restoration.restored_entities == (DEFAULT_TARGET,)


async def test_restore_timeout_is_bounded_and_consumed(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck reproducer times out and its RESTORING item is never replayed."""
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    restore_call_count = 0

    async def async_never_restore(_hass: HomeAssistant, _state: State) -> None:
        nonlocal restore_call_count
        restore_call_count += 1
        restore_started.set()
        await release_restore.wait()

    monkeypatch.setattr(
        "custom_components.presence_auto_off.controller.async_reproduce_state",
        async_never_restore,
    )
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    config = _rule_config(restore_on_presence=True)
    controller = await controller_factory(config, "restore-timeout-entry")
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    with patch(
        "custom_components.presence_auto_off.controller."
        "_TARGET_SERVICE_TIMEOUT_SECONDS",
        0.01,
    ):
        hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
        await asyncio.wait_for(restore_started.wait(), timeout=1)
        try:
            await hass.async_block_till_done()
        finally:
            release_restore.set()

    assert controller.restore_plan is None
    assert controller.last_restoration is not None
    assert controller.last_restoration.failed_entities == {
        DEFAULT_TARGET: "service_timeout"
    }
    assert restore_call_count == 1

    await controller.async_unload()
    restarted = await controller_factory(config, "restore-timeout-entry")
    assert restarted.restore_plan is None
    assert restore_call_count == 1


@pytest.mark.parametrize("crash_phase", ("prepared", "restoring"))
async def test_crash_uncertain_restore_phase_is_never_replayed(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
    crash_phase: str,
) -> None:
    """Persisted PREPARED/RESTORING is discarded without external action."""
    entry_id = f"uncertain-{crash_phase}-entry"
    config = _rule_config(restore_on_presence=True)
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    first = await controller_factory(config, entry_id)
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert first.restore_plan is not None
    ready_plan = first.restore_plan
    ready_item = ready_plan.items[0]
    await first.async_unload()

    if crash_phase == "prepared":
        uncertain_item = replace(
            ready_item,
            phase=RestoreItemPhase.PREPARED,
            after_off=None,
        )
    else:
        uncertain_item = replace(
            ready_item,
            phase=RestoreItemPhase.RESTORING,
        )
    uncertain_plan = replace(ready_plan, items=(uncertain_item,))
    stored = await first._store.async_load()
    assert stored is not None
    stored["restore_plan"] = uncertain_plan.as_dict()
    await first._store.async_save(stored)

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    restarted = await controller_factory(config, entry_id)

    assert restore_recorder.calls == []
    assert restarted.restore_plan is None
    assert restarted.last_restoration is not None
    assert restarted.last_restoration.skipped_entities == {
        DEFAULT_TARGET: "uncertain_after_restart"
    }


@pytest.mark.parametrize("skip_kind", ("gate", "target"))
async def test_skip_claim_save_failure_remains_ready_across_restart(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
    skip_kind: str,
) -> None:
    """A failed terminal claim never clears replayable READY only in memory."""
    entry_id = f"skip-claim-{skip_kind}-entry"
    if skip_kind == "gate":
        hass.states.async_set(SHABBAT_ENTITY, STATE_OFF)
        config = _rule_config(
            shabbat_entity=SHABBAT_ENTITY,
            allowed_day_types=frozenset({DayType.ORDINARY}),
            restore_on_presence=True,
        )
        expected_reason = "day_type_not_allowed"
    else:
        config = _rule_config(restore_on_presence=True)
        expected_reason = "unavailable"

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    first = await controller_factory(config, entry_id)
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert first.restore_plan is not None

    if skip_kind == "gate":
        hass.states.async_set(SHABBAT_ENTITY, STATE_ON)
    else:
        hass.states.async_set(DEFAULT_TARGET, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    original_save = first._store.async_save

    async def async_fail_restoring_claim(data: dict[str, object]) -> None:
        raw_plan = data.get("restore_plan")
        if isinstance(raw_plan, dict):
            items = raw_plan.get("items")
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("phase") == "restoring"
                for item in items
            ):
                raise OSError("claim journal unavailable")
        await original_save(data)

    with patch.object(
        first._store,
        "async_save",
        new=async_fail_restoring_claim,
    ):
        hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
        await hass.async_block_till_done()

    assert first.restore_plan is not None
    assert first.restore_plan.items[0].phase is RestoreItemPhase.READY
    assert first.last_restoration is None
    assert restore_recorder.calls == []
    await first.async_unload()

    restarted = await controller_factory(config, entry_id)
    assert restarted.restore_plan is None
    assert restarted.last_restoration is not None
    assert restarted.last_restoration.skipped_entities == {
        DEFAULT_TARGET: expected_reason
    }
    assert restore_recorder.calls == []


async def test_live_target_id_reuse_never_authorizes_replacement(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """A mutable entity ID cannot transfer shutdown authority to a new UUID."""
    registry = er.async_get(hass)
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config())
    selected_entry = registry.async_get(DEFAULT_TARGET)
    assert selected_entry is not None

    renamed_target = "light.renamed_room"
    registry.async_update_entity(DEFAULT_TARGET, new_entity_id=renamed_target)
    hass.states.async_remove(DEFAULT_TARGET)
    hass.states.async_set(renamed_target, STATE_ON)
    replacement = registry.async_get_or_create(
        "light",
        "replacement",
        "replacement-room-light",
        suggested_object_id="room",
    )
    assert replacement.entity_id == DEFAULT_TARGET
    registry.async_update_entity(DEFAULT_TARGET, area_id=controller.config.area_id)
    hass.states.async_set(DEFAULT_TARGET, STATE_ON)

    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    assert turn_off_recorder.calls == []
    assert hass.states.get(DEFAULT_TARGET) is not None
    assert hass.states.get(DEFAULT_TARGET).state == STATE_ON
    assert hass.states.get(renamed_target) is not None
    assert hass.states.get(renamed_target).state == STATE_ON
    assert controller.last_execution is not None
    assert controller.last_execution.failed_entities == {
        DEFAULT_TARGET: "selection_identity_changed"
    }


async def test_live_target_rename_is_not_restored_through_reused_id(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """Restore authority remains bound to UUID and setup-time current ID."""
    registry = er.async_get(hass)
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert controller.restore_plan is not None

    renamed_target = "light.renamed_restore_room"
    registry.async_update_entity(DEFAULT_TARGET, new_entity_id=renamed_target)
    hass.states.async_remove(DEFAULT_TARGET)
    hass.states.async_set(renamed_target, STATE_OFF)
    replacement = registry.async_get_or_create(
        "light",
        "replacement",
        "replacement-restore-light",
        suggested_object_id="room",
    )
    assert replacement.entity_id == DEFAULT_TARGET
    registry.async_update_entity(DEFAULT_TARGET, area_id=controller.config.area_id)
    hass.states.async_set(DEFAULT_TARGET, STATE_ON)

    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    assert restore_recorder.calls == []
    renamed_state = hass.states.get(renamed_target)
    replacement_state = hass.states.get(DEFAULT_TARGET)
    assert renamed_state is not None and renamed_state.state == STATE_OFF
    assert replacement_state is not None and replacement_state.state == STATE_ON
    assert controller.last_restoration is not None
    assert controller.last_restoration.skipped_entities == {
        renamed_target: "selection_identity_changed"
    }


async def test_live_presence_id_reuse_is_fail_closed(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """A replacement binary sensor cannot drive a rule configured for another UUID."""
    registry = er.async_get(hass)
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config())
    original_presence = registry.async_get(PRESENCE_ENTITY)
    assert original_presence is not None

    registry.async_update_entity(
        PRESENCE_ENTITY,
        new_entity_id="binary_sensor.renamed_room_presence",
    )
    hass.states.async_remove(PRESENCE_ENTITY)
    replacement = registry.async_get_or_create(
        "binary_sensor",
        "replacement",
        "replacement-presence",
        suggested_object_id="room_presence",
    )
    assert replacement.entity_id == PRESENCE_ENTITY
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    assert turn_off_recorder.calls == []
    assert controller.status is Status.SENSOR_UNAVAILABLE


@pytest.mark.parametrize("journal_phase", ("prepared", "restoring"))
async def test_disable_during_journal_save_prevents_external_action(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
    journal_phase: str,
) -> None:
    """Disable intent is visible before waiting for the controller lock."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))
    if journal_phase == "restoring":
        hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
        await hass.async_block_till_done()
        assert controller.restore_plan is not None

    original_save = controller._store.async_save
    journal_save_started = asyncio.Event()
    release_journal_save = asyncio.Event()
    delayed_once = False

    async def async_delay_journal(data: dict[str, object]) -> None:
        nonlocal delayed_once
        raw_plan = data.get("restore_plan")
        if not delayed_once and isinstance(raw_plan, dict):
            items = raw_plan.get("items")
            if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("phase") == journal_phase
                for item in items
            ):
                delayed_once = True
                journal_save_started.set()
                await release_journal_save.wait()
        await original_save(data)

    with patch.object(controller._store, "async_save", new=async_delay_journal):
        if journal_phase == "prepared":
            hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
        else:
            hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
        await asyncio.wait_for(journal_save_started.wait(), timeout=1)
        disable_task = asyncio.create_task(controller.async_set_enabled(False))
        await asyncio.sleep(0)
        assert controller._action_inhibited
        release_journal_save.set()
        await disable_task
        await hass.async_block_till_done()

    if journal_phase == "prepared":
        assert turn_off_recorder.calls == []
    else:
        assert restore_recorder.calls == []
    assert not controller.enabled


async def test_disable_discard_failure_restarts_from_nonreplayable_claim(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
    restore_recorder: RestoreRecorder,
) -> None:
    """A failed READY-removal save leaves durable RESTORING, never READY."""
    entry_id = "disable-discard-failure-entry"
    config = _rule_config(restore_on_presence=True)
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    first = await controller_factory(config, entry_id)
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()
    assert first.restore_plan is not None

    original_save = first._store.async_save
    failed_removal = False

    async def async_fail_ready_removal(data: dict[str, object]) -> None:
        nonlocal failed_removal
        if not failed_removal and data.get("restore_plan") is None:
            failed_removal = True
            raise OSError("terminal removal unavailable")
        await original_save(data)

    with (
        patch.object(
            first._store,
            "async_save",
            new=async_fail_ready_removal,
        ),
        pytest.raises(OSError, match="terminal removal unavailable"),
    ):
        await first.async_set_enabled(False)

    assert failed_removal
    assert first.restore_plan is None
    assert not first.enabled

    # Simulate a crash without another graceful storage write. The first phase
    # of the discard is already durable and must be filtered on setup.
    first._unsubscribe_state_changes()
    first._cancel_deadline_locked()
    first._unloaded = True
    first._setup_complete = False
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    restarted = await controller_factory(config, entry_id)

    assert restarted.restore_plan is None
    assert restore_recorder.calls == []


async def test_unserializable_after_off_snapshot_is_consumed_safely(
    hass: HomeAssistant,
    controller_factory: ControllerFactory,
    turn_off_recorder: TurnOffRecorder,
) -> None:
    """Non-JSON OFF attributes cannot strand PREPARED or abort accounting."""
    hass.states.async_set(PRESENCE_ENTITY, STATE_ON)
    controller = await controller_factory(_rule_config(restore_on_presence=True))

    async def async_off_with_bad_attributes(call: ServiceCall) -> None:
        entity_id = call.data[ATTR_ENTITY_ID]
        turn_off_recorder.calls.append(entity_id)
        hass.states.async_set(entity_id, STATE_OFF, {"not_json": object()})

    hass.services.async_register(
        "light",
        SERVICE_TURN_OFF,
        async_off_with_bad_attributes,
    )
    hass.states.async_set(PRESENCE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    assert controller.restore_plan is None
    assert controller.last_execution is not None
    assert controller.last_execution.failed_entities == {
        DEFAULT_TARGET: "after_off_snapshot_not_serializable"
    }
