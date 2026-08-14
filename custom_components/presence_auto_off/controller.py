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
    callback,
    split_entity_id,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
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
from .models import (
    ALL_DAY_TYPES,
    AbsenceEpisode,
    ActivityEvent,
    ActivityEventType,
    DayType,
    LastExecution,
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
        self._last_activity: ActivityEvent | None = None
        self._blocked_episode_id: str | None = None
        self._last_saved_payload: dict[str, Any] | None = None

        self._state_listeners: set[StateListener] = set()
        self._activity_listeners: set[ActivityListener] = set()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_deadline: CALLBACK_TYPE | None = None
        self._setup_complete = False
        self._unloaded = False

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
            activity: ActivityEvent | None = None
            async with self._lock:
                self._unloaded = False
                self._restore_locked(stored)
                self._refresh_gate_locked()
                plan, activity = self._initialize_from_current_state_locked()
                self._setup_complete = True
                await self._async_save_locked()

            self._notify_state_listeners()
            if activity is not None:
                self._publish_activity(activity)
            if plan is not None:
                await self._async_execute(plan)
        except BaseException:
            self._rollback_failed_setup()
            raise

    async def async_stop(self) -> None:
        """Stop automation work while retaining entity observers for rollback."""
        self._unsubscribe_state_changes()

        async with self._lock:
            if not self._unloaded:
                self._unloaded = True
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
            activity: ActivityEvent | None = None
            async with self._lock:
                self._unloaded = False
                self._refresh_gate_locked()
                plan, activity = self._initialize_from_current_state_locked()
                await self._async_save_locked()

            self._notify_state_listeners()
            if activity is not None:
                self._publish_activity(activity)
            if plan is not None:
                await self._async_execute(plan)
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
                if had_pending_episode:
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

        self._notify_state_listeners()
        if activity is not None:
            self._publish_activity(activity)
        if plan is not None:
            await self._async_execute(plan)

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle presence or day-gate state changes."""
        plan: _ExecutionPlan | None = None
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

            if entity_id == self.config.presence_entity:
                if not self._enabled:
                    # A completed episode is retained while disabled only as
                    # long as the same continuous OFF state remains in place.
                    if new_value != STATE_OFF or old_value != STATE_OFF:
                        self._clear_episode_locked()
                    self._status = Status.DISABLED
                elif new_value == STATE_ON:
                    if self._episode is not None and not self._episode.completed:
                        activity = self._new_activity_locked(
                            ActivityEventType.NO_ACTION,
                            {
                                "reason": "presence_returned",
                                "episode_id": self._episode.episode_id,
                            },
                        )
                    self._clear_episode_locked()
                    self._status = Status.OCCUPIED
                elif new_value == STATE_OFF:
                    # Attribute-only updates must not restart the delay.
                    if self._episode is None:
                        self._start_episode_locked(dt_util.utcnow())
                    plan, due_activity = self._reconcile_episode_locked(
                        dt_util.utcnow()
                    )
                    activity = due_activity or activity
                else:
                    if self._episode is not None and not self._episode.completed:
                        activity = self._new_activity_locked(
                            ActivityEventType.NO_ACTION,
                            {
                                "reason": "presence_sensor_unavailable",
                                "episode_id": self._episode.episode_id,
                            },
                        )
                    self._clear_episode_locked()
                    self._status = Status.SENSOR_UNAVAILABLE
            elif self._enabled:
                presence_value = self._presence_value()
                if presence_value is None:
                    self._clear_episode_locked()
                    self._status = Status.SENSOR_UNAVAILABLE
                elif presence_value == STATE_ON:
                    self._clear_episode_locked()
                    self._status = Status.OCCUPIED
                elif self._episode is None:
                    self._start_episode_locked(dt_util.utcnow())
                    plan, activity = self._reconcile_episode_locked(dt_util.utcnow())
                elif not self._episode.completed:
                    plan, activity = self._reconcile_episode_locked(dt_util.utcnow())

            await self._async_save_locked()

        self._notify_state_listeners()
        if activity is not None:
            self._publish_activity(activity)
        if plan is not None:
            await self._async_execute(plan)

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
            self._clear_episode_locked()
            self._status = (
                Status.DISABLED if not self._enabled else Status.SENSOR_UNAVAILABLE
            )
            return None, None

        if presence_value == STATE_ON:
            self._clear_episode_locked()
            self._status = Status.DISABLED if not self._enabled else Status.OCCUPIED
            return None, None

        if not self._enabled:
            self._status = Status.DISABLED
            return None, None

        if self._episode is None:
            self._start_episode_locked(dt_util.utcnow())

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
            self._clear_episode_locked()
            self._status = Status.SENSOR_UNAVAILABLE
            return None, None
        if presence_value == STATE_ON:
            self._clear_episode_locked()
            self._status = Status.OCCUPIED
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
                successful.append(entity_id)

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
        if registry_entry.disabled:
            return "disabled"
        if (
            self.config.area_id is None
            or er.async_get_effective_area_id(self.hass, registry_entry)
            != self.config.area_id
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
        if not self._enabled:
            return "controller_disabled"

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
        return tuple(
            dict.fromkeys(
                entity_id
                for entity_id in (
                    self.config.presence_entity,
                    self.config.shabbat_entity,
                    self.config.holiday_entity,
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
        self._blocked_episode_id = None
        self._setup_complete = False
        self._unloaded = True
        self._state_listeners.clear()
        self._activity_listeners.clear()

    def _start_episode_locked(self, now: datetime) -> None:
        """Start a new continuous-absence episode and its deadline."""
        self._cancel_deadline_locked()
        self._generation += 1
        deadline = now + timedelta(seconds=self.config.delay_seconds)
        self._episode = AbsenceEpisode(
            episode_id=uuid4().hex,
            generation=self._generation,
            presence_entity=self.config.presence_entity,
            started_at=now,
            deadline=deadline,
        )
        self._blocked_episode_id = None

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

    async def _async_save_locked(self, *, force: bool = False) -> None:
        """Persist all state required to resume safely after restart."""
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
        }
        if not force and payload == self._last_saved_payload:
            return
        await self._store.async_save(payload)
        self._last_saved_payload = payload

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
