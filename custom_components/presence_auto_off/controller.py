"""Event-driven controller for a Presence Auto-Off room rule."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.state import async_reproduce_state
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DATA,
    ATTR_ENTRY_ID,
    ATTR_EVENT_TYPE,
    ATTR_OCCURRED_AT,
    ATTR_RULE_ID,
    DEFAULT_ENABLED,
    EVENT_ACTIVITY,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from .helpers import effective_area_id
from .models import (
    ALL_DAY_TYPES,
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

_LOGGER = logging.getLogger(__name__)
_TARGET_SERVICE_TIMEOUT_SECONDS = 30.0

StateListener = Callable[[], None]
ActivityListener = Callable[[ActivityEvent], None]


@dataclass(frozen=True, slots=True)
class _ExecutionPlan:
    """An immutable execution prepared while holding the controller lock."""

    episode_id: str
    generation: int
    target_entities: tuple[str, ...]
    started_at: datetime


class PresenceAutoOffController:
    """Coordinate one room's absence countdown and target shutdown."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: RuleConfig,
    ) -> None:
        """Initialize a room controller."""
        if not config.presence_entity:
            raise ValueError("A presence entity is required")

        self.hass = hass
        self.entry_id = entry_id
        self.config = config if config.rule_id else replace(config, rule_id=entry_id)

        registry = er.async_get(hass)
        presence_registry_entry = registry.async_get(self.config.presence_entity)
        self._configured_presence_registry_id = (
            presence_registry_entry.id if presence_registry_entry is not None else None
        )
        self._configured_gate_registry_by_entity: dict[str, str] = {}
        for gate_entity_id in (
            self.config.shabbat_entity,
            self.config.holiday_entity,
        ):
            if gate_entity_id is None:
                continue
            if gate_registry_entry := registry.async_get(gate_entity_id):
                self._configured_gate_registry_by_entity[gate_entity_id] = (
                    gate_registry_entry.id
                )
        configured_target_registry_by_entity: dict[str, str] = {}
        for reference in self.config.target_entities:
            resolved_entity_id = er.async_resolve_entity_id(registry, reference)
            if resolved_entity_id is None:
                continue
            if registry_entry := registry.async_get(resolved_entity_id):
                configured_target_registry_by_entity[resolved_entity_id] = (
                    registry_entry.id
                )
        self._configured_target_registry_by_entity = (
            configured_target_registry_by_entity
        )
        self._configured_target_entity_by_registry = {
            registry_id: entity_id
            for entity_id, registry_id in configured_target_registry_by_entity.items()
        }

        self._store = Store[dict[str, Any]](
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        self._lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._execution_tasks: set[asyncio.Task[Any]] = set()
        self._execution_idle = asyncio.Event()
        self._execution_idle.set()

        self._enabled = DEFAULT_ENABLED
        self._status = Status.INITIALIZING
        self._day_type = DayType.UNKNOWN
        self._gate_allowed = False
        self._episode: AbsenceEpisode | None = None
        self._generation = 0
        self._last_execution: LastExecution | None = None
        self._restore_plan: RestorePlan | None = None
        self._pending_restore_discard = False
        self._last_restoration: LastRestoration | None = None
        self._last_activity: ActivityEvent | None = None
        self._blocked_episode_id: str | None = None
        self._last_saved_payload: dict[str, Any] | None = None

        self._state_listeners: set[StateListener] = set()
        self._activity_listeners: set[ActivityListener] = set()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_deadline: CALLBACK_TYPE | None = None
        self._setup_complete = False
        self._unloaded = False
        self._action_inhibited = not DEFAULT_ENABLED

    @property
    def enabled(self) -> bool:
        """Return whether automatic shutdown is enabled."""
        return self._enabled

    @property
    def status(self) -> Status:
        """Return the current controller status."""
        return self._status

    @property
    def day_type(self) -> DayType:
        """Return the current derived day type."""
        return self._day_type

    @property
    def gate_allowed(self) -> bool:
        """Return whether the current day gate permits execution."""
        return self._gate_allowed

    @property
    def deadline(self) -> datetime | None:
        """Return the current absence deadline."""
        if self._episode is None or self._episode.completed:
            return None
        return self._episode.deadline

    @property
    def absence_started_at(self) -> datetime | None:
        """Return when the current absence episode started."""
        return self._episode.started_at if self._episode is not None else None

    @property
    def episode_id(self) -> str | None:
        """Return the current absence episode ID."""
        return self._episode.episode_id if self._episode is not None else None

    @property
    def last_execution(self) -> LastExecution | None:
        """Return the latest persisted execution result."""
        return self._last_execution

    @property
    def restore_plan(self) -> RestorePlan | None:
        """Return pending restoration work."""
        return self._restore_plan

    @property
    def last_restoration(self) -> LastRestoration | None:
        """Return the latest persisted restoration result."""
        return self._last_restoration

    @property
    def last_activity(self) -> ActivityEvent | None:
        """Return the latest activity emitted in this runtime."""
        return self._last_activity

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for entities and diagnostics."""
        presence_state = self.hass.states.get(self.config.presence_entity)
        return {
            "entry_id": self.entry_id,
            "rule_id": self.config.rule_id,
            "config": self.config.as_dict(),
            "enabled": self.enabled,
            "status": self.status.value,
            "day_type": self.day_type.value,
            "gate_allowed": self.gate_allowed,
            "presence_state": (
                presence_state.state if presence_state is not None else None
            ),
            "absence_episode": (
                self._episode.as_dict() if self._episode is not None else None
            ),
            "last_execution": (
                self.last_execution.as_dict()
                if self.last_execution is not None
                else None
            ),
            # Snapshot attributes may contain sensitive device data. Diagnostics
            # intentionally expose only identity, phase, and safety markers.
            "pending_restoration": self._restore_plan_diagnostics(),
            "last_restoration": (
                self.last_restoration.as_dict()
                if self.last_restoration is not None
                else None
            ),
            "last_activity": (
                self.last_activity.as_dict() if self.last_activity is not None else None
            ),
        }

    @callback
    def async_add_listener(self, listener: StateListener) -> CALLBACK_TYPE:
        """Register a callback for observable state changes."""
        self._state_listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._state_listeners.discard(listener)

        return remove_listener

    @callback
    def async_add_activity_listener(self, listener: ActivityListener) -> CALLBACK_TYPE:
        """Register a callback for activity events."""
        self._activity_listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._activity_listeners.discard(listener)

        return remove_listener

    @callback
    def add_listener(self, listener: StateListener) -> CALLBACK_TYPE:
        """Register a state listener (non-prefixed compatibility alias)."""
        return self.async_add_listener(listener)

    @callback
    def add_activity_listener(self, listener: ActivityListener) -> CALLBACK_TYPE:
        """Register an activity listener (compatibility alias)."""
        return self.async_add_activity_listener(listener)

    async def async_setup(self) -> None:
        """Restore state, subscribe to entities, and reconcile current state."""
        if self._setup_complete:
            return

        try:
            stored = await self._store.async_load()
            self._subscribe_state_changes()

            plan: _ExecutionPlan | None = None
            restore_episode_id: str | None = None
            activity: ActivityEvent | None = None
            async with self._lock:
                self._unloaded = False
                self._restore_locked(stored)
                self._refresh_gate_locked()
                plan, activity = self._initialize_from_current_state_locked()
                restore_episode_id = self._restore_episode_if_due_locked()
                self._setup_complete = True
                await self._async_save_locked()
                self._action_inhibited = not self._enabled

            self._notify_state_listeners()
            if activity is not None:
                self._publish_activity(activity)
            if plan is not None:
                await self._async_execute(plan)
            if restore_episode_id is not None:
                await self._async_restore(restore_episode_id)
        except BaseException:
            self._rollback_failed_setup()
            raise

    async def async_stop(self) -> None:
        """Stop automation work while retaining entity observers for rollback."""
        self._unsubscribe_state_changes()

        # Publish the stop intent before waiting for the controller lock. A
        # write-ahead storage call may currently hold that lock; its mandatory
        # post-save safety check must be able to observe teardown immediately.
        if self._unloaded:
            await self._execution_idle.wait()
            return
        self._unloaded = True

        async with self._lock:
            self._invalidate_deadline_locked(preserve_episode=True)
            try:
                await self._async_save_locked(force=True)
            except Exception:
                # Teardown is safety-critical: a storage outage must not
                # leave callbacks running or prevent platform unloading.
                _LOGGER.warning(
                    "Could not persist stopped state for rule %s",
                    self.config.rule_id,
                    exc_info=True,
                )

        await self._execution_idle.wait()

    async def async_resume(self) -> None:
        """Resume after a platform unload was rejected."""
        if not self._setup_complete or not self._unloaded:
            return

        self._subscribe_state_changes()
        try:
            plan: _ExecutionPlan | None = None
            restore_episode_id: str | None = None
            activity: ActivityEvent | None = None
            async with self._lock:
                self._unloaded = False
                self._refresh_gate_locked()
                plan, activity = self._initialize_from_current_state_locked()
                restore_episode_id = self._restore_episode_if_due_locked()
                await self._async_save_locked()
                self._action_inhibited = not self._enabled

            self._notify_state_listeners()
            if activity is not None:
                self._publish_activity(activity)
            if plan is not None:
                await self._async_execute(plan)
            if restore_episode_id is not None:
                await self._async_restore(restore_episode_id)
        except BaseException:
            self._unsubscribe_state_changes()
            self._cancel_deadline_locked()
            self._unloaded = True
            raise

    async def async_unload(self) -> None:
        """Remove listeners, stop timers, and finish any in-flight execution."""
        await self.async_stop()
        self._setup_complete = False
        self._state_listeners.clear()
        self._activity_listeners.clear()

    async def async_set_enabled(self, enabled: bool) -> None:
        """Persist and apply the enabled switch without a config reload."""
        if not enabled:
            # A write-ahead save may currently hold `_lock`. Publish disable
            # intent synchronously so its post-save check cannot start a new
            # external action before this setter acquires the lock.
            self._action_inhibited = True

        plan: _ExecutionPlan | None = None
        activity: ActivityEvent | None = None
        async with self._lock:
            if self._unloaded or enabled == self._enabled:
                return

            self._enabled = enabled
            if not enabled:
                had_pending_episode = (
                    self._episode is not None and not self._episode.completed
                )
                pending_episode_id = self.episode_id
                self._invalidate_deadline_locked(preserve_episode=True)
                self._status = Status.DISABLED
                restore_activity = self._discard_restore_plan_locked(
                    "controller_disabled"
                )
                if restore_activity is not None:
                    activity = restore_activity
                elif had_pending_episode:
                    activity = self._new_activity_locked(
                        ActivityEventType.NO_ACTION,
                        {
                            "reason": "disabled",
                            "episode_id": pending_episode_id,
                        },
                    )
            else:
                self._refresh_gate_locked()
                plan, activity = self._initialize_from_current_state_locked()

            await self._async_save_locked()
            if enabled:
                # Clear only after enabled-state reconciliation was saved. Any
                # due plan executes after the lock with actions permitted.
                self._action_inhibited = False

        self._notify_state_listeners()
        if activity is not None:
            self._publish_activity(activity)
        if plan is not None:
            await self._async_execute(plan)

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle presence or day-gate state changes."""
        plan: _ExecutionPlan | None = None
        restore_episode_id: str | None = None
        activity: ActivityEvent | None = None

        async with self._lock:
            if not self._setup_complete or self._unloaded:
                return

            entity_id = event.data["entity_id"]
            old_state = event.data["old_state"]
            new_state = event.data["new_state"]
            old_value = old_state.state if old_state is not None else None
            new_value = new_state.state if new_state is not None else None
            self._refresh_gate_locked()

            if entity_id != self.config.presence_entity:
                self._mark_restore_item_modified_locked(event)

            if entity_id == self.config.presence_entity:
                if not self._enabled:
                    # A completed episode is retained while disabled only as
                    # long as the same continuous OFF state remains in place.
                    if new_value != STATE_OFF or old_value != STATE_OFF:
                        self._clear_episode_locked()
                    restore_activity = self._discard_restore_plan_locked(
                        "controller_disabled"
                    )
                    if restore_activity is not None:
                        activity = restore_activity
                    self._status = Status.DISABLED
                elif new_value == STATE_ON:
                    pending_episode_id = (
                        self._episode.episode_id
                        if self._episode is not None and not self._episode.completed
                        else None
                    )
                    self._clear_episode_locked()
                    restore_episode_id = self._restore_episode_if_due_locked()
                    if restore_episode_id is None and pending_episode_id is not None:
                        activity = self._new_activity_locked(
                            ActivityEventType.NO_ACTION,
                            {
                                "reason": "presence_returned",
                                "episode_id": pending_episode_id,
                            },
                        )
                    self._status = (
                        Status.RESTORING
                        if restore_episode_id is not None
                        else Status.OCCUPIED
                    )
                elif new_value == STATE_OFF:
                    # Attribute-only updates must not restart the delay.
                    if self._episode is None:
                        self._start_episode_locked(
                            self._off_state_started_at(new_state)
                        )
                    plan, due_activity = self._reconcile_episode_locked(
                        dt_util.utcnow()
                    )
                    activity = due_activity or activity
                else:
                    restore_activity = self._discard_restore_plan_locked(
                        "presence_sensor_unavailable"
                    )
                    if self._episode is not None and not self._episode.completed:
                        activity = self._new_activity_locked(
                            ActivityEventType.NO_ACTION,
                            {
                                "reason": "presence_sensor_unavailable",
                                "episode_id": self._episode.episode_id,
                            },
                        )
                    if restore_activity is not None:
                        activity = restore_activity
                    self._clear_episode_locked()
                    self._status = Status.SENSOR_UNAVAILABLE
            elif self._enabled:
                presence_value = self._presence_value()
                if presence_value is None:
                    restore_activity = self._discard_restore_plan_locked(
                        "presence_sensor_unavailable"
                    )
                    if restore_activity is not None:
                        activity = restore_activity
                    self._clear_episode_locked()
                    self._status = Status.SENSOR_UNAVAILABLE
                elif presence_value == STATE_ON:
                    self._clear_episode_locked()
                    restore_episode_id = self._restore_episode_if_due_locked()
                    self._status = (
                        Status.RESTORING
                        if restore_episode_id is not None
                        else Status.OCCUPIED
                    )
                elif self._episode is None:
                    self._start_episode_locked(self._off_state_started_at())
                    plan, activity = self._reconcile_episode_locked(dt_util.utcnow())
                elif not self._episode.completed:
                    plan, activity = self._reconcile_episode_locked(dt_util.utcnow())

            await self._async_save_locked()

        self._notify_state_listeners()
        if activity is not None:
            self._publish_activity(activity)
        if plan is not None:
            await self._async_execute(plan)
        if restore_episode_id is not None:
            await self._async_restore(restore_episode_id)

    async def _async_deadline_reached(self, generation: int, _now: datetime) -> None:
        """Handle a deadline timer, rejecting stale generations."""
        plan: _ExecutionPlan | None = None
        activity: ActivityEvent | None = None
        async with self._lock:
            if self._unloaded:
                return
            episode = self._episode
            if episode is None or episode.generation != generation:
                return
            self._unsub_deadline = None
            self._refresh_gate_locked()
            plan, activity = self._reconcile_episode_locked(dt_util.as_utc(_now))
            await self._async_save_locked()

        self._notify_state_listeners()
        if activity is not None:
            self._publish_activity(activity)
        if plan is not None:
            await self._async_execute(plan)

    def _initialize_from_current_state_locked(
        self,
    ) -> tuple[_ExecutionPlan | None, ActivityEvent | None]:
        """Reconcile restored data with the current presence state."""
        presence_value = self._presence_value()

        if presence_value is None:
            activity = self._discard_restore_plan_locked("presence_sensor_unavailable")
            self._clear_episode_locked()
            self._status = (
                Status.DISABLED if not self._enabled else Status.SENSOR_UNAVAILABLE
            )
            return None, activity

        if presence_value == STATE_ON:
            activity = None
            if not self._enabled:
                activity = self._discard_restore_plan_locked("controller_disabled")
            self._clear_episode_locked()
            self._status = Status.DISABLED if not self._enabled else Status.OCCUPIED
            return None, activity

        if not self._enabled:
            activity = self._discard_restore_plan_locked("controller_disabled")
            self._status = Status.DISABLED
            return None, activity

        if self._episode is None:
            self._start_episode_locked(self._off_state_started_at())

        return self._reconcile_episode_locked(dt_util.utcnow())

    def _reconcile_episode_locked(
        self, now: datetime
    ) -> tuple[_ExecutionPlan | None, ActivityEvent | None]:
        """Schedule, block, or prepare the current episode for execution."""
        episode = self._episode
        if episode is None:
            return None, None

        if not self._enabled:
            self._status = Status.DISABLED
            return None, None

        presence_value = self._presence_value()
        if presence_value is None:
            activity = self._discard_restore_plan_locked("presence_sensor_unavailable")
            self._clear_episode_locked()
            self._status = Status.SENSOR_UNAVAILABLE
            return None, activity
        if presence_value == STATE_ON:
            self._clear_episode_locked()
            self._status = (
                Status.RESTORING
                if self._restore_episode_if_due_locked() is not None
                else Status.OCCUPIED
            )
            return None, None

        if episode.completed:
            self._cancel_deadline_locked()
            self._status = (
                Status.ERROR
                if self._last_execution is not None
                and self._last_execution.episode_id == episode.episode_id
                and not self._last_execution.succeeded
                else Status.COMPLETED
            )
            return None, None

        if now < episode.deadline:
            self._schedule_deadline_locked(episode)
            self._status = (
                Status.SENSOR_UNAVAILABLE
                if self._day_type is DayType.UNKNOWN and not self._gate_allowed
                else Status.COUNTDOWN
            )
            return None, None

        self._cancel_deadline_locked()
        if not self._gate_allowed:
            sensor_unavailable = self._day_type is DayType.UNKNOWN
            self._status = (
                Status.SENSOR_UNAVAILABLE
                if sensor_unavailable
                else Status.WAITING_CONDITION
            )
            if self._blocked_episode_id == episode.episode_id:
                return None, None
            self._blocked_episode_id = episode.episode_id
            return None, self._new_activity_locked(
                ActivityEventType.BLOCKED,
                {
                    "reason": (
                        "gate_sensor_unavailable"
                        if sensor_unavailable
                        else "day_type_not_allowed"
                    ),
                    "episode_id": episode.episode_id,
                    "day_type": self._day_type.value,
                    "deadline": episode.deadline.isoformat(),
                },
            )

        completed_at = dt_util.utcnow()
        self._episode = replace(episode, completed=True, completed_at=completed_at)
        self._status = Status.EXECUTING
        self._blocked_episode_id = None
        return (
            _ExecutionPlan(
                episode_id=episode.episode_id,
                generation=episode.generation,
                target_entities=self.config.target_entities,
                started_at=completed_at,
            ),
            None,
        )

    async def _async_execute(self, plan: _ExecutionPlan) -> None:
        """Serialize and account for every prepared execution attempt."""
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("Execution must run in an asyncio task")

        self._execution_tasks.add(current_task)
        self._execution_idle.clear()
        try:
            async with self._execution_lock:
                await self._async_execute_serialized(plan)
        finally:
            self._execution_tasks.discard(current_task)
            if not self._execution_tasks:
                self._execution_idle.set()

    async def _async_execute_serialized(self, plan: _ExecutionPlan) -> None:
        """Call each target domain's turn_off service independently."""
        successful: list[str] = []
        failed: dict[str, str] = {}

        for index, entity_id in enumerate(plan.target_entities):
            prepared_registry_id: str | None = None
            async with self._lock:
                stop_reason = self._execution_stop_reason_locked(plan)
            if stop_reason is not None:
                for skipped in plan.target_entities[index:]:
                    failed[skipped] = stop_reason
                break

            target_error = self._target_runtime_error(entity_id)
            if target_error is not None:
                failed[entity_id] = target_error
                continue

            pre_state = self.hass.states.get(entity_id)
            if pre_state is None:
                failed[entity_id] = "missing"
                continue

            if self.config.restore_on_presence and pre_state.state != STATE_OFF:
                registry_entry = er.async_get(self.hass).async_get(entity_id)
                if registry_entry is None:
                    failed[entity_id] = "missing"
                    continue
                try:
                    before = self._snapshot_state(pre_state)
                    prepared = RestoreItem(
                        registry_entry_id=registry_entry.id,
                        entity_id_at_capture=entity_id,
                        before=before,
                        phase=RestoreItemPhase.PREPARED,
                    )
                except (TypeError, ValueError):
                    failed[entity_id] = "state_snapshot_not_serializable"
                    _LOGGER.warning(
                        "Could not safely snapshot %s for rule %s",
                        entity_id,
                        self.config.rule_id,
                        exc_info=True,
                    )
                    continue

                try:
                    async with self._lock:
                        stop_reason = self._execution_stop_reason_locked(plan)
                        if stop_reason is None:
                            stop_reason = self._target_runtime_error(entity_id)
                        pre_save_state = self.hass.states.get(entity_id)
                        if stop_reason is None and pre_save_state is not pre_state:
                            stop_reason = "state_changed_before_shutdown"
                        if stop_reason is None:
                            self._append_restore_item_locked(plan, prepared)
                            # Write-ahead is mandatory: never change a device if
                            # its original state is not durably recoverable.
                            await self._async_save_locked(force=True)
                            prepared_registry_id = registry_entry.id

                            # Storage is an await point. Revalidate every safety
                            # input and the exact captured state before changing
                            # the device, so a manual action during write-ahead
                            # can never be undone on later presence.
                            stop_reason = self._execution_stop_reason_locked(plan)
                            if stop_reason is None:
                                stop_reason = self._target_runtime_error(entity_id)
                            current_state = self.hass.states.get(entity_id)
                            if stop_reason is None and (
                                current_state is None
                                or current_state is not pre_save_state
                                or current_state.state != pre_state.state
                                or current_state.attributes != pre_state.attributes
                            ):
                                stop_reason = "state_changed_before_shutdown"

                            if stop_reason is not None:
                                self._remove_restore_item_locked(
                                    registry_entry.id,
                                    phases=frozenset({RestoreItemPhase.PREPARED}),
                                )
                                try:
                                    await self._async_save_locked(force=True)
                                except Exception:
                                    # No external action follows. A durable
                                    # PREPARED record is safely discarded on
                                    # startup if cleanup could not be saved.
                                    _LOGGER.warning(
                                        "Could not clear stopped restore preparation for %s",
                                        entity_id,
                                        exc_info=True,
                                    )
                                prepared_registry_id = None
                except Exception as err:
                    async with self._lock:
                        self._remove_restore_item_locked(registry_entry.id)
                    failed[entity_id] = "restore_snapshot_persist_failed"
                    _LOGGER.warning(
                        "Could not persist the restore snapshot for %s in rule %s: %s",
                        entity_id,
                        self.config.rule_id,
                        err,
                    )
                    continue

                if stop_reason is not None:
                    failed[entity_id] = stop_reason
                    continue

            try:
                domain, _object_id = split_entity_id(entity_id)
                async with asyncio.timeout(_TARGET_SERVICE_TIMEOUT_SECONDS):
                    await self.hass.services.async_call(
                        domain,
                        SERVICE_TURN_OFF,
                        {ATTR_ENTITY_ID: entity_id},
                        blocking=True,
                    )
            except TimeoutError:
                failed[entity_id] = "service_timeout"
                _LOGGER.warning(
                    "Timed out turning off %s for rule %s",
                    entity_id,
                    self.config.rule_id,
                )
            except Exception as err:
                message = f"{type(err).__name__}: {err}"
                failed[entity_id] = message[:500]
                _LOGGER.warning(
                    "Failed to turn off %s for rule %s: %s",
                    entity_id,
                    self.config.rule_id,
                    message,
                )
            else:
                if prepared_registry_id is not None:
                    off_state = self.hass.states.get(entity_id)
                    if off_state is None or off_state.state != STATE_OFF:
                        failed[entity_id] = "turn_off_not_confirmed"
                    else:
                        try:
                            after_off = self._snapshot_state(off_state)
                        except (TypeError, ValueError):
                            failed[entity_id] = "after_off_snapshot_not_serializable"
                            _LOGGER.warning(
                                "Could not safely snapshot the OFF state for %s in rule %s",
                                entity_id,
                                self.config.rule_id,
                                exc_info=True,
                            )
                        else:
                            try:
                                async with self._lock:
                                    if self._mark_restore_ready_locked(
                                        prepared_registry_id, after_off
                                    ):
                                        await self._async_save_locked(force=True)
                                        successful.append(entity_id)
                                    else:
                                        failed[entity_id] = (
                                            "restore_plan_discarded_during_shutdown"
                                        )
                            except Exception as err:
                                failed[entity_id] = "restore_ready_persist_failed"
                                _LOGGER.warning(
                                    "Could not persist restore readiness for %s in rule %s: %s",
                                    entity_id,
                                    self.config.rule_id,
                                    err,
                                )
                else:
                    successful.append(entity_id)

            if prepared_registry_id is not None and entity_id in failed:
                async with self._lock:
                    removed = self._remove_restore_item_locked(
                        prepared_registry_id,
                        phases=frozenset({RestoreItemPhase.PREPARED}),
                    )
                    if removed:
                        try:
                            await self._async_save_locked(force=True)
                        except Exception:
                            # A PREPARED record is deliberately crash-uncertain
                            # and is discarded rather than acted on at startup.
                            _LOGGER.warning(
                                "Could not clear an uncertain restore journal for %s",
                                entity_id,
                                exc_info=True,
                            )

        execution = LastExecution(
            episode_id=plan.episode_id,
            occurred_at=plan.started_at,
            successful_entities=tuple(successful),
            failed_entities=failed,
        )

        if not plan.target_entities:
            event_type = ActivityEventType.NO_ACTION
            reason = "no_targets"
        elif failed:
            event_type = ActivityEventType.FAILED
            reason = "partial_or_total_failure"
        else:
            event_type = ActivityEventType.EXECUTED
            reason = "targets_turned_off"

        async with self._lock:
            self._last_execution = execution
            episode = self._episode
            if (
                episode is not None
                and episode.episode_id == plan.episode_id
                and episode.generation == plan.generation
                and self._enabled
                and not self._unloaded
            ):
                self._status = Status.ERROR if failed else Status.COMPLETED
            activity = self._new_activity_locked(
                event_type,
                {
                    "reason": reason,
                    "episode_id": plan.episode_id,
                    "successful_entities": list(successful),
                    "failed_entities": dict(failed),
                    "partial_failure": bool(successful and failed),
                },
            )
            await self._async_save_locked()

        self._notify_state_listeners()
        self._publish_activity(activity)

    async def _async_restore(self, episode_id: str) -> None:
        """Serialize and account for one presence-triggered restoration."""
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("Restoration must run in an asyncio task")

        self._execution_tasks.add(current_task)
        self._execution_idle.clear()
        try:
            async with self._execution_lock:
                await self._async_restore_serialized(episode_id)
        finally:
            self._execution_tasks.discard(current_task)
            if not self._execution_tasks:
                self._execution_idle.set()

    async def _async_restore_serialized(self, episode_id: str) -> None:
        """Restore READY items once, one target at a time."""
        restored: list[str] = []
        skipped: dict[str, str] = {}
        failed: dict[str, str] = {}
        had_plan = False
        preserve_plan = False

        while True:
            current_entity_id: str | None = None
            item: RestoreItem | None = None
            async with self._lock:
                plan = self._restore_plan
                if plan is None or plan.episode_id != episode_id:
                    break
                had_plan = True

                stop_reason = self._restore_stop_reason_locked()
                if stop_reason == "controller_unloaded":
                    preserve_plan = True
                    break
                if stop_reason is not None:
                    original_plan = plan
                    claimed_items = tuple(
                        replace(item, phase=RestoreItemPhase.RESTORING)
                        if item.phase is RestoreItemPhase.READY
                        else item
                        for item in plan.items
                    )
                    if claimed_items != plan.items:
                        self._restore_plan = replace(plan, items=claimed_items)
                        try:
                            # Claim every replayable READY item before a
                            # terminal whole-plan skip. If the later removal
                            # save fails, durable RESTORING remains one-shot.
                            await self._async_save_locked(force=True)
                        except Exception:
                            self._restore_plan = original_plan
                            preserve_plan = True
                            _LOGGER.warning(
                                "Could not claim blocked restoration for rule %s",
                                self.config.rule_id,
                                exc_info=True,
                            )
                            break
                    for remaining in plan.items:
                        skipped[remaining.entity_id_at_capture] = stop_reason
                    self._restore_plan = None
                    try:
                        await self._async_save_locked(force=True)
                    except Exception:
                        _LOGGER.warning(
                            "Could not persist discarded restoration for rule %s",
                            self.config.rule_id,
                            exc_info=True,
                        )
                    break

                item = plan.items[0]
                if item.phase is not RestoreItemPhase.READY:
                    skipped[item.entity_id_at_capture] = "uncertain_restore_phase"
                    self._remove_restore_item_locked(item.registry_entry_id)
                    try:
                        await self._async_save_locked(force=True)
                    except Exception:
                        _LOGGER.warning(
                            "Could not clear uncertain restore work for %s",
                            item.entity_id_at_capture,
                            exc_info=True,
                        )
                    continue

                current_entity_id, target_error = self._restore_target_error_locked(
                    item
                )
                outcome_entity_id = current_entity_id or item.entity_id_at_capture
                if target_error is not None:
                    restoring_item = replace(
                        item,
                        phase=RestoreItemPhase.RESTORING,
                    )
                    self._replace_restore_item_locked(restoring_item)
                    try:
                        # A target validation skip is terminal too. Persist a
                        # non-replayable claim before clearing READY.
                        await self._async_save_locked(force=True)
                    except Exception:
                        self._replace_restore_item_locked(item)
                        preserve_plan = True
                        _LOGGER.warning(
                            "Could not claim skipped restoration for %s",
                            outcome_entity_id,
                            exc_info=True,
                        )
                        break
                    skipped[outcome_entity_id] = target_error
                    self._remove_restore_item_locked(item.registry_entry_id)
                    try:
                        await self._async_save_locked(force=True)
                    except Exception:
                        _LOGGER.warning(
                            "Could not persist skipped restoration for %s",
                            outcome_entity_id,
                            exc_info=True,
                        )
                    continue

                assert current_entity_id is not None
                restore_pre_save_state = self.hass.states.get(current_entity_id)
                restoring_item = replace(
                    item,
                    phase=RestoreItemPhase.RESTORING,
                )
                self._replace_restore_item_locked(restoring_item)
                try:
                    # RESTORING is written before the external action. A crash
                    # can therefore never cause an automatic second attempt.
                    await self._async_save_locked(force=True)
                except Exception as err:
                    self._replace_restore_item_locked(item)
                    failed[current_entity_id] = "restore_journal_persist_failed"
                    preserve_plan = True
                    _LOGGER.warning(
                        "Could not persist restore intent for %s in rule %s: %s",
                        current_entity_id,
                        self.config.rule_id,
                        err,
                    )
                    break

                # The journal save above yielded control. Recheck all live
                # inputs before the external restore, then consume RESTORING if
                # anything became unsafe. It must never be reverted to READY.
                stop_reason = self._restore_stop_reason_locked()
                post_save_entity_id, target_error = self._restore_target_error_locked(
                    restoring_item
                )
                post_save_state = (
                    self.hass.states.get(post_save_entity_id)
                    if post_save_entity_id is not None
                    else None
                )
                if target_error is None and (
                    post_save_entity_id != current_entity_id
                    or post_save_state is not restore_pre_save_state
                ):
                    target_error = "state_changed_since_shutdown"
                if stop_reason is not None or target_error is not None:
                    outcome_entity_id = post_save_entity_id or item.entity_id_at_capture
                    skipped[outcome_entity_id] = stop_reason or target_error
                    self._remove_restore_item_locked(item.registry_entry_id)
                    try:
                        await self._async_save_locked(force=True)
                    except Exception:
                        # The already-durable RESTORING marker is crash-safe.
                        _LOGGER.warning(
                            "Could not persist post-journal restore skip for %s",
                            outcome_entity_id,
                            exc_info=True,
                        )
                    continue
                assert post_save_entity_id is not None
                current_entity_id = post_save_entity_id

            assert item is not None
            assert current_entity_id is not None
            restore_error: str | None = None
            try:
                desired_state = State(
                    current_entity_id,
                    item.before.state,
                    item.before.attributes,
                )
                async with asyncio.timeout(_TARGET_SERVICE_TIMEOUT_SECONDS):
                    await async_reproduce_state(self.hass, desired_state)
            except TimeoutError:
                restore_error = "service_timeout"
                _LOGGER.warning(
                    "Timed out restoring %s for rule %s",
                    current_entity_id,
                    self.config.rule_id,
                )
            except Exception as err:
                restore_error = f"{type(err).__name__}: {err}"[:500]
                _LOGGER.warning(
                    "Failed to restore %s for rule %s: %s",
                    current_entity_id,
                    self.config.rule_id,
                    restore_error,
                )
            else:
                current_state = self.hass.states.get(current_entity_id)
                if current_state is None or current_state.state != item.before.state:
                    restore_error = "restore_not_confirmed"

            async with self._lock:
                # Consume RESTORING regardless of the service result. If this
                # save fails, the durable RESTORING marker is itself fail-safe.
                self._remove_restore_item_locked(item.registry_entry_id)
                if restore_error is None:
                    restored.append(current_entity_id)
                else:
                    failed[current_entity_id] = restore_error
                try:
                    await self._async_save_locked(force=True)
                except Exception:
                    _LOGGER.warning(
                        "Could not persist the consumed restore item for %s",
                        current_entity_id,
                        exc_info=True,
                    )

        if not had_plan and not restored and not skipped and not failed:
            return
        if preserve_plan and not restored and not skipped and not failed:
            return

        activity: ActivityEvent | None = None
        async with self._lock:
            restoration = LastRestoration(
                episode_id=episode_id,
                occurred_at=dt_util.utcnow(),
                restored_entities=tuple(restored),
                skipped_entities=skipped,
                failed_entities=failed,
            )
            self._last_restoration = restoration

            if failed:
                event_type = ActivityEventType.RESTORE_FAILED
                reason = "partial_or_total_failure"
            elif restored:
                event_type = ActivityEventType.RESTORED
                reason = "states_restored"
            else:
                event_type = ActivityEventType.RESTORE_SKIPPED
                reason = "restore_skipped"

            if self._presence_value() == STATE_ON and self._enabled:
                self._status = Status.ERROR if failed else Status.OCCUPIED
            activity = self._new_activity_locked(
                event_type,
                {
                    "reason": reason,
                    "episode_id": episode_id,
                    "restored_entities": list(restored),
                    "skipped_entities": dict(skipped),
                    "failed_entities": dict(failed),
                    "partial_failure": bool(restored and failed),
                    "pending_entities": (
                        [
                            pending.entity_id_at_capture
                            for pending in self._restore_plan.items
                        ]
                        if self._restore_plan is not None
                        and self._restore_plan.episode_id == episode_id
                        else []
                    ),
                },
            )
            try:
                await self._async_save_locked(force=True)
            except Exception:
                _LOGGER.warning(
                    "Could not persist restoration result for rule %s",
                    self.config.rule_id,
                    exc_info=True,
                )

        self._notify_state_listeners()
        if activity is not None:
            self._publish_activity(activity)

    @staticmethod
    def _snapshot_state(
        state: State, *, attributes: bool = True
    ) -> EntityStateSnapshot:
        """Capture a detached, JSON-safe primary state."""
        return EntityStateSnapshot(
            state=state.state,
            attributes=state.attributes if attributes else {},
            captured_at=dt_util.utcnow(),
        )

    def _append_restore_item_locked(
        self, execution: _ExecutionPlan, item: RestoreItem
    ) -> None:
        """Append a write-ahead item to this episode's restore plan."""
        plan = self._restore_plan
        if plan is not None and plan.episode_id != execution.episode_id:
            raise RuntimeError("A previous restoration is still pending")

        if plan is None:
            self._restore_plan = RestorePlan(
                episode_id=execution.episode_id,
                presence_entity=self._stable_entity_reference(
                    self.config.presence_entity
                ),
                created_at=dt_util.utcnow(),
                day_type_at_shutdown=self._day_type,
                items=(item,),
            )
            return

        items = (
            *(
                existing
                for existing in plan.items
                if existing.registry_entry_id != item.registry_entry_id
            ),
            item,
        )
        self._restore_plan = replace(plan, items=items)

    def _replace_restore_item_locked(self, item: RestoreItem) -> bool:
        """Replace an existing restore item by stable registry identity."""
        plan = self._restore_plan
        if plan is None:
            return False
        found = False
        items: list[RestoreItem] = []
        for existing in plan.items:
            if existing.registry_entry_id == item.registry_entry_id:
                items.append(item)
                found = True
            else:
                items.append(existing)
        if found:
            self._restore_plan = replace(plan, items=tuple(items))
        return found

    def _remove_restore_item_locked(
        self,
        registry_entry_id: str,
        *,
        phases: frozenset[RestoreItemPhase] | None = None,
    ) -> bool:
        """Remove matching restore work, deleting an empty plan."""
        plan = self._restore_plan
        if plan is None:
            return False
        retained: list[RestoreItem] = []
        removed = False
        for item in plan.items:
            if item.registry_entry_id != registry_entry_id or (
                phases is not None and item.phase not in phases
            ):
                retained.append(item)
            else:
                removed = True
        if not removed:
            return False
        self._restore_plan = replace(plan, items=tuple(retained)) if retained else None
        return True

    def _mark_restore_ready_locked(
        self, registry_entry_id: str, after_off: EntityStateSnapshot
    ) -> bool:
        """Promote a durable PREPARED item after confirmed shutdown."""
        plan = self._restore_plan
        if plan is None:
            return False
        for item in plan.items:
            if (
                item.registry_entry_id == registry_entry_id
                and item.phase is RestoreItemPhase.PREPARED
            ):
                return self._replace_restore_item_locked(
                    replace(
                        item,
                        phase=RestoreItemPhase.READY,
                        after_off=after_off,
                    )
                )
        return False

    def _restore_episode_if_due_locked(self) -> str | None:
        """Return pending work only while presence is affirmatively ON."""
        plan = self._restore_plan
        if (
            plan is None
            or self._pending_restore_discard
            or not self.config.restore_on_presence
            or not self._enabled
            or self._action_inhibited
            or self._presence_value() != STATE_ON
        ):
            return None
        self._status = Status.RESTORING
        return plan.episode_id

    def _stable_entity_reference(self, entity_id: str) -> str:
        """Return a registry UUID when possible, otherwise the entity ID."""
        registry_entry = er.async_get(self.hass).async_get(entity_id)
        return registry_entry.id if registry_entry is not None else entity_id

    def _same_entity_identity(self, reference: str, entity_id: str) -> bool:
        """Compare a stored registry reference with a current entity ID."""
        registry_entry = er.async_get(self.hass).async_get(entity_id)
        if reference == entity_id:
            # A raw reference denotes an input that was unregistered when the
            # plan was created. Do not let a later registry owner inherit it.
            return registry_entry is None
        return registry_entry is not None and registry_entry.id == reference

    def _input_identity_matches(
        self, entity_id: str, expected_registry_id: str | None
    ) -> bool:
        """Validate a configured sensor without breaking unregistered inputs."""
        registry_entry = er.async_get(self.hass).async_get(entity_id)
        if expected_registry_id is None:
            # YAML/template inputs may intentionally have no registry entry.
            # If an owner later claims this mutable ID, fail closed until reload.
            return registry_entry is None
        return registry_entry is not None and registry_entry.id == expected_registry_id

    def _restore_stop_reason_locked(self) -> str | None:
        """Return a fail-closed reason before the next restore action."""
        if self._unloaded:
            return "controller_unloaded"
        if not self.config.restore_on_presence:
            return "restore_feature_disabled"
        if self._action_inhibited or not self._enabled:
            return "controller_disabled"

        presence_value = self._presence_value()
        if presence_value is None:
            return "presence_sensor_unavailable"
        if presence_value != STATE_ON:
            return "presence_changed_during_restore"

        self._refresh_gate_locked()
        if not self._gate_allowed:
            return (
                "gate_sensor_unavailable"
                if self._day_type is DayType.UNKNOWN
                else "day_type_not_allowed"
            )
        return None

    def _restore_target_error_locked(
        self, item: RestoreItem
    ) -> tuple[str | None, str | None]:
        """Resolve and validate one READY target without external I/O."""
        registry = er.async_get(self.hass)
        entity_id = er.async_resolve_entity_id(registry, item.registry_entry_id)
        if entity_id is None:
            return None, "missing"
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None:
            return entity_id, "missing"
        configured_entity_id = self._configured_target_entity_by_registry.get(
            item.registry_entry_id
        )
        if configured_entity_id is None:
            return entity_id, "no_longer_selected"
        if entity_id != configured_entity_id:
            return entity_id, "selection_identity_changed"
        if registry_entry.disabled:
            return entity_id, "disabled"
        if (
            self.config.area_id is None
            or effective_area_id(self.hass, registry_entry) != self.config.area_id
        ):
            return entity_id, "out_of_area"
        if item.after_off is None or item.after_off.state != STATE_OFF:
            return entity_id, "shutdown_state_uncertain"

        current = self.hass.states.get(entity_id)
        if current is None:
            return entity_id, "missing"
        if current.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return entity_id, "unavailable"
        if item.modified_since_off:
            return entity_id, "modified_since_shutdown"
        try:
            current_snapshot = self._snapshot_state(current)
        except (TypeError, ValueError):
            return entity_id, "state_changed_since_shutdown"
        if (
            current_snapshot.state != item.after_off.state
            or current_snapshot.attributes != item.after_off.attributes
        ):
            return entity_id, "state_changed_since_shutdown"
        return entity_id, None

    def _mark_restore_item_modified_locked(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Remember any entity change made after a confirmed shutdown."""
        plan = self._restore_plan
        new_state = event.data["new_state"]
        if plan is None:
            return

        event_entity_id = event.data["entity_id"]
        registry_entry = er.async_get(self.hass).async_get(event_entity_id)
        registry_id = registry_entry.id if registry_entry is not None else None
        for item in plan.items:
            if item.phase is not RestoreItemPhase.READY or item.after_off is None:
                continue
            if (
                item.registry_entry_id != registry_id
                and item.entity_id_at_capture != event_entity_id
            ):
                continue
            # A state-change task emitted by our own turn_off may run after the
            # item was promoted to READY. Its timestamp predates capture. Every
            # later primary-state, attribute, availability, removal, or
            # reappearance event is conservatively treated as manual intent.
            if (
                new_state is not None
                and new_state.last_updated <= item.after_off.captured_at
            ):
                return
            self._replace_restore_item_locked(replace(item, modified_since_off=True))
            return

    def _discard_restore_plan_locked(self, reason: str) -> ActivityEvent | None:
        """Stage a durable non-replayable claim for pending work."""
        plan = self._restore_plan
        if plan is None:
            return None
        skipped = {item.entity_id_at_capture: reason for item in plan.items}
        self._restore_plan = replace(
            plan,
            items=tuple(
                replace(item, phase=RestoreItemPhase.RESTORING)
                if item.phase is RestoreItemPhase.READY
                else item
                for item in plan.items
            ),
        )
        self._pending_restore_discard = True
        self._last_restoration = LastRestoration(
            episode_id=plan.episode_id,
            occurred_at=dt_util.utcnow(),
            skipped_entities=skipped,
        )
        return self._new_activity_locked(
            ActivityEventType.RESTORE_SKIPPED,
            {
                "reason": reason,
                "episode_id": plan.episode_id,
                "restored_entities": [],
                "skipped_entities": skipped,
                "failed_entities": {},
            },
        )

    def _restore_plan_diagnostics(self) -> dict[str, Any] | None:
        """Return pending restore metadata without captured state attributes."""
        plan = self._restore_plan
        if plan is None:
            return None
        return {
            "episode_id": plan.episode_id,
            "presence_entity": plan.presence_entity,
            "created_at": plan.created_at.isoformat(),
            "day_type_at_shutdown": plan.day_type_at_shutdown.value,
            "items": [
                {
                    "registry_entry_id": item.registry_entry_id,
                    "entity_id_at_capture": item.entity_id_at_capture,
                    "phase": item.phase.value,
                    "modified_since_off": item.modified_since_off,
                }
                for item in plan.items
            ],
        }

    @callback
    def _target_runtime_error(self, entity_id: str) -> str | None:
        """Validate that a target is still safe and actionable."""
        try:
            domain, _object_id = split_entity_id(entity_id)
        except ValueError:
            return "invalid_entity_id"

        registry_entry = er.async_get(self.hass).async_get(entity_id)
        if registry_entry is None:
            return "missing"
        expected_registry_id = self._configured_target_registry_by_entity.get(entity_id)
        if expected_registry_id is None or registry_entry.id != expected_registry_id:
            return "selection_identity_changed"
        if registry_entry.disabled:
            return "disabled"
        if (
            self.config.area_id is None
            or effective_area_id(self.hass, registry_entry) != self.config.area_id
        ):
            return "out_of_area"
        if not self.hass.services.has_service(domain, SERVICE_TURN_OFF):
            return "unsupported_service"

        state = self.hass.states.get(entity_id)
        if state is None:
            return "missing"
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return "unavailable"
        return None

    def _execution_stop_reason_locked(self, plan: _ExecutionPlan) -> str | None:
        """Return why an in-flight execution must stop, if anything."""
        if self._unloaded:
            return "controller_unloaded"
        if self._action_inhibited or not self._enabled:
            return "controller_disabled"

        if (
            self.config.restore_on_presence
            and self._restore_plan is not None
            and self._restore_plan.episode_id != plan.episode_id
        ):
            return "pending_restoration_conflict"

        episode = self._episode
        if (
            episode is None
            or episode.episode_id != plan.episode_id
            or episode.generation != plan.generation
        ):
            return "absence_episode_changed"
        if self._presence_value() != STATE_OFF:
            return "presence_changed_during_execution"

        self._refresh_gate_locked()
        if not self._gate_allowed:
            return "condition_changed_during_execution"
        return None

    @callback
    def _tracked_entities(self) -> tuple[str, ...]:
        """Return the unique state entities observed by this rule."""
        restore_targets = (
            self.config.target_entities if self.config.restore_on_presence else ()
        )
        return tuple(
            dict.fromkeys(
                entity_id
                for entity_id in (
                    self.config.presence_entity,
                    self.config.shabbat_entity,
                    self.config.holiday_entity,
                    *restore_targets,
                )
                if entity_id is not None
            )
        )

    @callback
    def _subscribe_state_changes(self) -> None:
        """Subscribe once to the rule's input entities."""
        if self._unsub_state is None:
            self._unsub_state = async_track_state_change_event(
                self.hass, self._tracked_entities(), self._async_state_changed
            )

    @callback
    def _unsubscribe_state_changes(self) -> None:
        """Remove the input subscription if present."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    @callback
    def _rollback_failed_setup(self) -> None:
        """Make a partially initialized controller inert without more I/O."""
        self._unsubscribe_state_changes()
        self._cancel_deadline_locked()
        self._generation += 1
        self._episode = None
        self._restore_plan = None
        self._pending_restore_discard = False
        self._blocked_episode_id = None
        self._setup_complete = False
        self._unloaded = True
        self._state_listeners.clear()
        self._activity_listeners.clear()

    def _start_episode_locked(self, started_at: datetime) -> None:
        """Start an episode at the presence sensor's OFF transition."""
        self._cancel_deadline_locked()
        self._generation += 1
        deadline = started_at + timedelta(seconds=self.config.delay_seconds)
        self._episode = AbsenceEpisode(
            episode_id=uuid4().hex,
            generation=self._generation,
            presence_entity=self.config.presence_entity,
            started_at=started_at,
            deadline=deadline,
        )
        self._blocked_episode_id = None

    def _off_state_started_at(self, state: State | None = None) -> datetime:
        """Return when the current OFF state began, failing safely on bad time."""
        current_state = state or self.hass.states.get(self.config.presence_entity)
        now = dt_util.utcnow()
        if current_state is None or current_state.state != STATE_OFF:
            return now

        changed_at = dt_util.as_utc(current_state.last_changed)
        # A future timestamp caused by clock skew must never postpone shutdown
        # beyond the full configured delay measured from the present.
        return min(changed_at, now)

    def _clear_episode_locked(self) -> None:
        """Invalidate and clear the current absence episode."""
        self._cancel_deadline_locked()
        self._generation += 1
        self._episode = None
        self._blocked_episode_id = None

    def _invalidate_deadline_locked(self, *, preserve_episode: bool) -> None:
        """Invalidate callbacks, optionally retaining episode continuity."""
        self._cancel_deadline_locked()
        self._generation += 1
        if self._episode is None:
            return
        if preserve_episode:
            self._episode = replace(self._episode, generation=self._generation)
        else:
            self._episode = None
            self._blocked_episode_id = None

    def _schedule_deadline_locked(self, episode: AbsenceEpisode) -> None:
        """Schedule one deadline callback for the current generation."""
        if self._unsub_deadline is not None:
            return

        generation = episode.generation

        async def deadline_reached(now: datetime) -> None:
            await self._async_deadline_reached(generation, now)

        self._unsub_deadline = async_track_point_in_utc_time(
            self.hass, deadline_reached, episode.deadline
        )

    def _cancel_deadline_locked(self) -> None:
        """Cancel the active deadline callback."""
        if self._unsub_deadline is not None:
            self._unsub_deadline()
            self._unsub_deadline = None

    def _presence_value(self) -> str | None:
        """Return ON/OFF, or None when the presence sensor is unusable."""
        if not self._input_identity_matches(
            self.config.presence_entity,
            self._configured_presence_registry_id,
        ):
            return None
        state = self.hass.states.get(self.config.presence_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        if state.state not in (STATE_ON, STATE_OFF):
            return None
        return state.state

    def _refresh_gate_locked(self) -> None:
        """Derive day type and the fail-closed gate decision."""
        sensor_states: dict[str, str] = {}
        for entity_id in (
            self.config.shabbat_entity,
            self.config.holiday_entity,
        ):
            if entity_id is None:
                continue
            if not self._input_identity_matches(
                entity_id,
                self._configured_gate_registry_by_entity.get(entity_id),
            ):
                self._day_type = DayType.UNKNOWN
                self._gate_allowed = self.config.allowed_day_types >= ALL_DAY_TYPES
                return
            state = self.hass.states.get(entity_id)
            if (
                state is None
                or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                or state.state not in (STATE_ON, STATE_OFF)
            ):
                self._day_type = DayType.UNKNOWN
                self._gate_allowed = self.config.allowed_day_types >= ALL_DAY_TYPES
                return
            sensor_states[entity_id] = state.state

        if (
            self.config.holiday_entity is not None
            and sensor_states.get(self.config.holiday_entity) == STATE_ON
        ):
            self._day_type = DayType.HOLIDAY
        elif (
            self.config.shabbat_entity is not None
            and sensor_states.get(self.config.shabbat_entity) == STATE_ON
        ):
            self._day_type = DayType.SHABBAT
        else:
            self._day_type = DayType.ORDINARY

        self._gate_allowed = self._day_type in self.config.allowed_day_types

    def _restore_locked(self, stored: Mapping[str, Any] | None) -> None:
        """Restore persisted state, ignoring incompatible or malformed data."""
        if stored is None:
            return

        stored_enabled = stored.get("enabled")
        if isinstance(stored_enabled, bool):
            self._enabled = stored_enabled
            self._action_inhibited = not stored_enabled

        raw_generation = stored.get("episode_generation")
        if isinstance(raw_generation, int) and not isinstance(raw_generation, bool):
            self._generation = max(0, raw_generation)

        raw_episode = stored.get("absence_episode")
        if isinstance(raw_episode, Mapping):
            episode = AbsenceEpisode.from_dict(raw_episode)
            if (
                episode is not None
                and episode.presence_entity == self.config.presence_entity
            ):
                # An options reload retains continuous absence, while applying
                # the newly configured duration to the original start time.
                self._episode = replace(
                    episode,
                    deadline=episode.started_at
                    + timedelta(seconds=self.config.delay_seconds),
                )
                self._generation = max(self._generation, episode.generation)

        raw_execution = stored.get("last_execution")
        if isinstance(raw_execution, Mapping):
            self._last_execution = LastExecution.from_dict(raw_execution)

        raw_restoration = stored.get("last_restoration")
        if isinstance(raw_restoration, Mapping):
            self._last_restoration = LastRestoration.from_dict(raw_restoration)

        raw_restore_plan = stored.get("restore_plan")
        if not isinstance(raw_restore_plan, Mapping):
            return
        restore_plan = RestorePlan.from_dict(raw_restore_plan)
        if restore_plan is None:
            return
        discard_reason: str | None = None
        if not self.config.restore_on_presence:
            discard_reason = "restore_feature_disabled"
        elif not self._same_entity_identity(
            restore_plan.presence_entity, self.config.presence_entity
        ):
            discard_reason = "presence_entity_changed"
        if discard_reason is not None:
            self._last_restoration = LastRestoration(
                episode_id=restore_plan.episode_id,
                occurred_at=dt_util.utcnow(),
                skipped_entities={
                    item.entity_id_at_capture: discard_reason
                    for item in restore_plan.items
                },
            )
            return

        ready_items = tuple(
            item for item in restore_plan.items if item.phase is RestoreItemPhase.READY
        )
        uncertain_items = tuple(
            item
            for item in restore_plan.items
            if item.phase is not RestoreItemPhase.READY
        )
        if ready_items:
            self._restore_plan = replace(restore_plan, items=ready_items)
        if uncertain_items:
            # PREPARED may have crashed before or after turn_off, while
            # RESTORING may have crashed before or after reproduction. Neither
            # phase is safe to replay automatically.
            self._last_restoration = LastRestoration(
                episode_id=restore_plan.episode_id,
                occurred_at=dt_util.utcnow(),
                skipped_entities={
                    item.entity_id_at_capture: "uncertain_after_restart"
                    for item in uncertain_items
                },
            )

    async def _async_save_locked(self, *, force: bool = False) -> None:
        """Persist all state required to resume safely after restart."""
        payload = self._storage_payload_locked()
        if self._pending_restore_discard:
            # First durably replace every READY item with RESTORING. Only then
            # may the terminal discard clear the plan. A failure of the second
            # write leaves a crash-safe non-replayable journal on disk.
            await self._store.async_save(payload)
            self._last_saved_payload = payload
            self._restore_plan = None
            self._pending_restore_discard = False
            payload = self._storage_payload_locked()
            await self._store.async_save(payload)
            self._last_saved_payload = payload
            return

        if not force and payload == self._last_saved_payload:
            return
        await self._store.async_save(payload)
        self._last_saved_payload = payload

    def _storage_payload_locked(self) -> dict[str, Any]:
        """Build the complete private storage payload."""
        payload: dict[str, Any] = {
            "enabled": self._enabled,
            "presence_entity": self.config.presence_entity,
            "episode_generation": self._generation,
            "absence_episode": (
                self._episode.as_dict() if self._episode is not None else None
            ),
            # Kept at top level as well for simple storage inspection.
            "deadline": (
                self._episode.deadline.isoformat()
                if self._episode is not None
                else None
            ),
            "last_execution": (
                self._last_execution.as_dict()
                if self._last_execution is not None
                else None
            ),
            "restore_plan": (
                self._restore_plan.as_dict() if self._restore_plan is not None else None
            ),
            "last_restoration": (
                self._last_restoration.as_dict()
                if self._last_restoration is not None
                else None
            ),
        }
        return payload

    def _new_activity_locked(
        self, event_type: ActivityEventType, data: Mapping[str, Any]
    ) -> ActivityEvent:
        """Create and retain a JSON-serializable activity event."""
        activity = ActivityEvent(
            event_type=event_type,
            occurred_at=dt_util.utcnow(),
            data=dict(data),
        )
        self._last_activity = activity
        return activity

    @callback
    def _notify_state_listeners(self) -> None:
        """Notify entity-style listeners without allowing one to block others."""
        for listener in tuple(self._state_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception(
                    "Error in state listener for Presence Auto-Off rule %s",
                    self.config.rule_id,
                )

    @callback
    def _publish_activity(self, activity: ActivityEvent) -> None:
        """Notify callbacks and publish a Home Assistant bus event."""
        for listener in tuple(self._activity_listeners):
            try:
                listener(activity)
            except Exception:
                _LOGGER.exception(
                    "Error in activity listener for Presence Auto-Off rule %s",
                    self.config.rule_id,
                )

        self.hass.bus.async_fire(
            EVENT_ACTIVITY,
            {
                ATTR_ENTRY_ID: self.entry_id,
                ATTR_RULE_ID: self.config.rule_id,
                ATTR_EVENT_TYPE: activity.event_type.value,
                ATTR_OCCURRED_AT: activity.occurred_at.isoformat(),
                ATTR_DATA: dict(activity.data),
            },
        )


# Concise compatibility alias for callers that prefer the domain name.
PresenceAutoOff = PresenceAutoOffController
