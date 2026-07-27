"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)

try:
    from homeassistant.components.vacuum import Segment
except ImportError:
    Segment = None  # HA < 2026.3 — room cleaning unavailable
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import CLEAN_MODE_MAP, FAN_SPEED_LIST, FAN_SPEED_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CleanMode, CommandResult, WorkingStatus

_LOGGER = logging.getLogger(__name__)

# Why the robot rejected a command, in plain language. Used both in the log
# and in the error surfaced to whoever called the service.
COMMAND_RESULT_HINTS: dict[int, str] = {
    CommandResult.NOT_APPLICABLE: (
        "the robot cannot run this command in its current state"
    ),
    CommandResult.CONFLICT: (
        "the robot is busy (cleaning, returning, or running a dock cycle)"
    ),
    CommandResult.NOT_READY: (
        "the robot declined to start — let it charge first. Rejections were "
        "observed below ~30% battery; a running mop-drying cycle does not "
        "block a start"
    ),
}


def describe_command_result(resp: Any) -> str:
    """Return '<NAME> (code=<n>): <hint>' for a command response."""
    code = resp.result_code
    try:
        name = CommandResult(code).name
    except ValueError:
        name = f"UNKNOWN({code})"
    hint = COMMAND_RESULT_HINTS.get(code)
    return f"{name} (code={code})" + (f": {hint}" if hint else "")


WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.DOCKED: VacuumActivity.DOCKED,
    WorkingStatus.CHARGED: VacuumActivity.DOCKED,
    WorkingStatus.DOCKED_V2: VacuumActivity.DOCKED,
    WorkingStatus.STANDBY: VacuumActivity.IDLE,
    WorkingStatus.CLEANING: VacuumActivity.CLEANING,
    WorkingStatus.CLEANING_ALT: VacuumActivity.CLEANING,
    WorkingStatus.TASK_COMPLETED: VacuumActivity.RETURNING,
    WorkingStatus.ERROR: VacuumActivity.ERROR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal vacuum entity."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalVacuum(coordinator)])

    # Custom service: clean a drawn rectangular zone (no built-in vacuum
    # service covers this). Zones are always robot WORLD coordinates — what
    # the card sends with camera calibration on the HD map. The legacy
    # 'coordinates' field is still accepted (so pre-2.0 calls don't error)
    # but ignored.
    # Imported here (not at module top) so the module still imports under the
    # lightweight test stubs that lack these HA helpers.
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers import entity_platform

    platform = entity_platform.async_get_current_platform()
    # `zone` is a list of rectangles [x1, y1, x2, y2] (floats accepted and
    # rounded), in robot world coordinates. Individual x1..y2 are also
    # accepted for manual/script calls (wrapped into one rect).
    platform.async_register_entity_service(
        "clean_zone",
        {
            vol.Optional("zone"): vol.All(
                cv.ensure_list,
                [vol.All([vol.Coerce(float)], vol.Length(min=4))],
            ),
            vol.Optional("x1"): vol.Coerce(float),
            vol.Optional("y1"): vol.Coerce(float),
            vol.Optional("x2"): vol.Coerce(float),
            vol.Optional("y2"): vol.Coerce(float),
            vol.Optional("fan_speed"): cv.string,
            # Removed in 2.0.0: accepted (so pre-2.0 calls still validate)
            # but ignored — zones are always world coordinates now.
            vol.Optional("coordinates"): cv.string,
        },
        "async_clean_zone",
    )
    # Clean specific rooms by segment id via clean/start_clean (honors the
    # selected clean mode). Works around the HA Segment API needing area
    # mapping, which is not configured.
    platform.async_register_entity_service(
        "clean_rooms",
        {
            vol.Required("rooms"): vol.All(
                cv.ensure_list, [vol.Coerce(int)], vol.Length(min=1)
            ),
        },
        "async_clean_rooms",
    )
    # Resume the current task unconditionally via task/resume. vacuum.start
    # only resumes when the coordinator reports CLEANING+paused, but during
    # real stuck events the broadcast state can lag behind (e.g. still
    # "docked"), sending the play button down the new-clean path instead.
    platform.async_register_entity_service(
        "resume",
        {},
        "async_resume_task",
    )


class NarwalVacuum(NarwalEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "vacuum"
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
    ) | (VacuumEntityFeature.CLEAN_AREA if Segment is not None else VacuumEntityFeature(0))
    _attr_fan_speed_list = FAN_SPEED_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the vacuum entity."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.data["device_id"]
        self._last_fan_speed: str | None = None

    @property
    def activity(self) -> VacuumActivity:
        """Return the current vacuum activity."""
        state = self.coordinator.data
        if state is None:
            return VacuumActivity.IDLE
        # is_paused (field 3.2) stays stale after docking — only trust it
        # while a clean is actually running. is_cleaning_session covers both
        # the legacy CLEANING(4) shape and the fw v01.08.03+ point-navi shape
        # (working_status stays DOCKED_V2 off dock), so a robot paused on the
        # new firmware is reported as PAUSED instead of falling through to
        # CLEANING. Paused takes priority over returning since the robot
        # physically stops when paused mid-return.
        if state.is_paused and state.is_cleaning_session:
            return VacuumActivity.PAUSED
        # Check returning before cleaning — robot keeps working_status=CLEANING
        # while navigating back to dock (field 3.7=1 indicates returning)
        if state.is_returning:
            return VacuumActivity.RETURNING
        if state.is_cleaning:
            return VacuumActivity.CLEANING
        if state.is_docked:
            return VacuumActivity.DOCKED
        activity = WORKING_STATUS_TO_ACTIVITY.get(state.working_status)
        # A dock-ish working_status that is_docked vetoed via the dock
        # indicator fields (fw v01.08.03+ point-navi keeps DOCKED_V2 while
        # driving) means the robot is active off dock — not on the dock.
        if activity == VacuumActivity.DOCKED:
            return VacuumActivity.CLEANING
        if activity is not None:
            return activity
        # Unknown working_status value — infer from dock signals so we
        # don't report IDLE while the robot is clearly active off-dock.
        # New firmware versions may introduce values we haven't mapped yet.
        if not state.is_docked:
            _LOGGER.warning(
                "Unmapped working_status %s (%d) while off-dock — reporting CLEANING",
                state.working_status.name, state.working_status.value,
            )
            return VacuumActivity.CLEANING
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str | None:
        """Return the current fan speed.

        The robot protocol does not broadcast the active fan speed setting,
        so we track the last value set via the integration. Returns None
        until the user sets a fan speed for the first time.
        """
        return self._last_fan_speed

    # Timeout for action commands (start/stop/return) — robot may need
    # time to load map, plan route, etc., especially after waking.
    _ACTION_TIMEOUT = 10.0

    @property
    def _clean_mode(self) -> CleanMode:
        """Clean mode selected via the clean-mode select entity."""
        return CLEAN_MODE_MAP.get(self.coordinator.clean_mode, CleanMode.SWEEP_MOP)

    def _command_error(self, action: str, resp: Any) -> HomeAssistantError:
        """Build the error raised when the robot rejects a command.

        Failed start commands used to be logged and swallowed, so a tap on a
        map card or an automation step looked successful while the robot never
        moved. Raising surfaces the reason in the UI and fails the automation
        step instead. `narwal.resume` is deliberately exempt: it is meant to be
        sent blind and the robot rejects it whenever there is nothing to resume.
        """
        detail = describe_command_result(resp)
        if resp.result_code == CommandResult.NOT_READY:
            state = self.coordinator.data
            if state is not None and state.battery_level:
                detail += f" (battery is at {state.battery_level}%)"
        return HomeAssistantError(f"{action} failed: {detail}")

    async def _ensure_awake(self) -> None:
        """Wake the robot if it is not broadcasting.

        Sends a wake burst and waits for broadcasts. If the robot doesn't
        respond, the command is still attempted — it may work even without
        a wake confirmation (e.g., shallow sleep).
        """
        client = self.coordinator.client
        if not client.robot_awake:
            _LOGGER.debug("Robot not awake — sending wake burst")
            await client.wake(timeout=10.0)

    async def async_start(self) -> None:
        """Start or resume cleaning."""
        await self._ensure_awake()
        state = self.coordinator.data
        # If a clean is paused, the play button must resume it, not start a
        # fresh whole-house clean. is_cleaning_session recognizes a paused task
        # on both firmware shapes (CLEANING(4) and fw v01.08.03+ DOCKED_V2 off
        # dock); is_paused alone stays stale after docking, so gate on both.
        if state and state.is_cleaning_session and state.is_paused:
            await self.coordinator.client.resume(timeout=self._ACTION_TIMEOUT)
            return

        client = self.coordinator.client
        mode = self._clean_mode
        # Primary path: clean/start_clean carries the mode (clean/plan/start
        # ignores it on fw v01.07+). Falls back to the legacy start() if the
        # robot rejects the whole-map start_clean payload.
        resp = await client.start_clean_whole(clean_mode=mode)
        if resp.result_code == CommandResult.NOT_APPLICABLE:
            _LOGGER.info(
                "Whole-map start_clean rejected (mode=%s); falling back to start()",
                mode.name,
            )
            resp = await client.start(clean_mode=mode)
        _LOGGER.info(
            "Start command response: code=%s, success=%s",
            resp.result_code, resp.success,
        )
        if not resp.success:
            _LOGGER.warning(
                "Start command did not succeed: %s", describe_command_result(resp),
            )
            raise self._command_error("Start", resp)

    async def async_stop(self, **kwargs) -> None:
        """Stop cleaning."""
        await self._ensure_awake()
        resp = await self.coordinator.client.stop()
        _LOGGER.info("Stop response: code=%s, success=%s", resp.result_code, resp.success)

    async def async_pause(self) -> None:
        """Pause cleaning."""
        resp = await self.coordinator.client.pause()
        _LOGGER.info("Pause response: code=%s, success=%s", resp.result_code, resp.success)

    async def async_return_to_base(self, **kwargs) -> None:
        """Return to the dock."""
        await self._ensure_awake()
        resp = await self.coordinator.client.return_to_base(timeout=self._ACTION_TIMEOUT)
        _LOGGER.info(
            "Return-to-base response: code=%s, success=%s",
            resp.result_code, resp.success,
        )
        if not resp.success:
            _LOGGER.warning(
                "Return-to-base did not succeed (code=%s)", resp.result_code,
            )

    async def async_locate(self, **kwargs) -> None:
        """Locate the vacuum — robot says 'Robot is here'."""
        await self._ensure_awake()
        await self.coordinator.client.locate()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs) -> None:
        """Set the fan speed."""
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is not None:
            await self.coordinator.client.set_fan_speed(level)
            self._last_fan_speed = fan_speed
            self.async_write_ha_state()

    # --- Segment API (HA 2026.3 room-specific cleaning) ---

    async def async_get_segments(self) -> list:
        """Return cleanable room segments from map data.

        Maps RoomInfo from get_map to HA Segment objects.
        Room names match the Narwal app exactly (RoomInfo.display_name).
        Falls back to HA-cached last_seen_segments when map data is not yet
        loaded (robot asleep at startup), so clean_area works without waking
        the robot first.
        Returns [] when HA < 2026.3 (Segment class unavailable).
        """
        if Segment is None:
            return []
        state = self.coordinator.data
        if state is None or state.map_data is None:
            # Robot sleeping — return cached segments so clean_area still works
            last = getattr(self, "last_seen_segments", None)
            return list(last) if last else []
        return [
            Segment(
                id=str(room.room_id),
                name=room.display_name,
                group="Rooms" if room.category == 1 else "Utility" if room.category == 2 else None,
            )
            for room in state.map_data.rooms
            if room.room_id > 0
        ]

    async def async_clean_segments(
        self, segment_ids: list[str], **kwargs: Any
    ) -> None:
        """Clean specific rooms by segment IDs.

        Converts string segment IDs back to integer room IDs and sends
        a room-specific clean command to the robot.
        """
        await self._ensure_awake()
        room_ids = [int(sid) for sid in segment_ids]
        await self._clean_rooms_with_mode(room_ids)

    async def async_clean_rooms(self, rooms: list[int], **kwargs: Any) -> None:
        """Clean specific rooms (by segment id) in the selected mode."""
        await self._ensure_awake()
        await self._clean_rooms_with_mode([int(r) for r in rooms])

    async def async_resume_task(self, **kwargs: Any) -> None:
        """Resume the current task via task/resume regardless of state.

        task/resume never starts a new task, so it is safe to send blind —
        the robot simply rejects it when there is nothing to resume.
        """
        await self._ensure_awake()
        resp = await self.coordinator.client.resume(timeout=self._ACTION_TIMEOUT)
        _LOGGER.info(
            "Resume response: code=%s, success=%s", resp.result_code, resp.success,
        )
        if not resp.success:
            # Sent blind by design (see async_resume_task) — a rejection is a
            # normal "nothing to resume", so this one only logs.
            _LOGGER.warning(
                "Resume command did not succeed: %s — "
                "the robot may have no paused task to resume",
                describe_command_result(resp),
            )

    async def _clean_rooms_with_mode(self, room_ids: list[int]) -> None:
        """Send a room clean in the selected mode via clean/start_clean.

        Falls back to the legacy start_rooms() path (clean/plan/start) if the
        robot rejects the start_clean room payload.
        """
        mode = self._clean_mode
        _LOGGER.info(
            "Starting room clean via start_clean: rooms=%s, mode=%s",
            room_ids, mode.name,
        )
        client = self.coordinator.client
        resp = await client.start_clean_rooms(room_ids, clean_mode=mode)
        if resp.result_code == CommandResult.NOT_APPLICABLE:
            _LOGGER.info(
                "Room start_clean rejected (rooms=%s); falling back to start_rooms",
                room_ids,
            )
            resp = await client.start_rooms(room_ids, clean_mode=mode)
        detail = describe_command_result(resp)
        _LOGGER.info("Room clean response: %s, rooms=%s", detail, room_ids)
        if not resp.success:
            _LOGGER.warning("Room clean failed: %s, rooms=%s", detail, room_ids)
            raise self._command_error("Room clean", resp)

    async def async_clean_zone(
        self,
        zone: list[list[int]] | None = None,
        x1: int | None = None, y1: int | None = None,
        x2: int | None = None, y2: int | None = None,
        fan_speed: str | None = None,
        coordinates: str | None = None, **kwargs: Any,
    ) -> None:
        """Clean one or more rectangular zones drawn on the map.

        `zone` is the xiaomi-vacuum-map-card [[selection]] format: a list of
        rectangles, each [x1, y1, x2, y2] in ROBOT WORLD (map-frame)
        coordinates — what the card sends with
        ``calibration_source: {camera: true}`` using the HD camera's
        ``calibration_points`` attribute (grid coordinate = world - origin).
        If `zone` is omitted, x1..y2 form a single rectangle. Corner order
        does not matter (start_zone normalizes min/max).

        `coordinates` (removed in 2.0.0): the legacy map-image pixel contract
        is gone — zones are always world coordinates. The parameter is still
        accepted so pre-2.0 service calls keep validating, but its value is
        ignored.

        The robot must be docked; start_zone retries briefly over the
        dock-settling transition.
        """
        zones_world: list[tuple[int, int, int, int]] = []
        if zone:
            for r in zone:
                zones_world.append((
                    round(float(r[0])), round(float(r[1])),
                    round(float(r[2])), round(float(r[3])),
                ))
        elif None not in (x1, y1, x2, y2):
            zones_world.append((
                round(float(x1)), round(float(y1)),
                round(float(x2)), round(float(y2)),
            ))
        if not zones_world:
            _LOGGER.warning("clean_zone: no rectangle given (zone or x1..y2)")
            return

        await self._ensure_awake()

        kw: dict[str, Any] = {"clean_mode": self._clean_mode}
        if fan_speed and fan_speed in FAN_SPEED_MAP:
            kw["fan"] = int(FAN_SPEED_MAP[fan_speed])
        _LOGGER.info(
            "clean_zone: world zones %s, mode=%s",
            zones_world, self._clean_mode.name,
        )
        resp = await self.coordinator.client.start_zone(zones_world, **kw)
        detail = describe_command_result(resp)
        _LOGGER.info("clean_zone response: %s, zones=%s", detail, zones_world)
        if not resp.success:
            _LOGGER.warning("clean_zone failed: %s, zones=%s", detail, zones_world)
            raise self._command_error("Zone clean", resp)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._check_segment_changes()
        super()._handle_coordinator_update()

    def _check_segment_changes(self) -> None:
        """Detect segment changes and raise repair issue if needed.

        Compares current room data against last_seen_segments (managed by HA).
        If rooms have changed (added, removed, or renamed), creates a repair
        issue so the user can update their segment-to-area mappings.
        """
        last = getattr(self, "last_seen_segments", None)
        if last is None:
            return  # No mapping configured yet
        state = self.coordinator.data
        if state is None or state.map_data is None:
            return
        current_set = {
            (str(r.room_id), r.display_name)
            for r in state.map_data.rooms
            if r.room_id > 0
        }
        last_set = {(s.id, s.name) for s in last}
        if current_set != last_set:
            _LOGGER.info(
                "Segment change detected: %d -> %d rooms",
                len(last_set), len(current_set),
            )
            self.async_create_segments_issue()
