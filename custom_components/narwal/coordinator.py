"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_CLEAN_MODE, DOMAIN
from .narwal_client import NarwalClient, NarwalConnectionError, NarwalState
from .narwal_client.const import WorkingStatus

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)

# Fast re-poll when state is incomplete (robot asleep at startup)
FAST_POLL_INTERVAL = timedelta(seconds=10)
FAST_POLL_MAX = 6  # up to 60s of fast polling before falling back to normal

# Cleaning trail (shared by all map cameras). Positions are recorded with
# full float precision on every display_map broadcast (~1.5s) that moved
# the robot at least _TRAIL_MIN_DIST grid cells — dense while driving,
# nothing while parked.
TRAIL_MAX_POINTS = 50000  # full cleaning session worth
TRAIL_MIN_DIST = 0.5  # grid cells (~3 cm) between recorded points

# Vacuumed strip accumulated from display_map field 12 rails (quads between
# the two parallel rail polylines) — cleared with the trail on new sessions.
SWATH_QUADS_MAX = 20000

# Lidar wall/obstacle observations accumulated from display_map field 7 —
# bounded by the map size. Non-carpet cells are cleared on each new cleaning
# session (fresh scan); CARPET cells (value & LIDAR_CARPET_FLAG) persist across
# sessions as a stable property of the home, clearing only when the map changes.
LIDAR_CELLS_MAX = 60000
LIDAR_CARPET_FLAG = 0x04  # low-byte bit2 = carpet (see map_renderer)


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Push-mode coordinator for Narwal vacuum.

    Primary data source is WebSocket broadcasts (every ~1.5s when awake).
    Fallback polling every 60s via get_status() in case broadcasts stop.

    Setup is kept fast: connect, try a few commands (which may time out if
    the robot is asleep), then start the listener. The listener's keepalive
    loop handles waking the robot — no blocking wake call during setup.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        product_key = entry.data.get("product_key")
        topic_prefix = f"/{product_key}" if product_key else None
        self.client = NarwalClient(
            host=entry.data["host"],
            port=entry.data["port"],
            device_id=entry.data.get("device_id", ""),
            topic_prefix=topic_prefix,
        )
        self._listen_task: asyncio.Task[None] | None = None
        # Clean mode option key (see CLEAN_MODE_MAP) applied to the next
        # start / room clean. Owned by the select entity; the protocol has
        # no standalone set-mode command, so it's HA-side state only.
        self.clean_mode: str = DEFAULT_CLEAN_MODE
        self._fast_poll_remaining = 0
        self._prev_working_status = WorkingStatus.UNKNOWN
        self._map_fetch_pending = False
        # Cleaning trail in grid coordinates — single source shared by all
        # map camera entities. Cleared when a new cleaning session starts.
        self.trail: list[tuple[float, float]] = []
        self._trail_last: tuple[float, float] | None = None
        self._was_cleaning_session = False
        # Vacuumed strip quads (grid coords, from field 12 rails) and the
        # lidar wall observations (grid cells, from field 7).
        self.swath_quads: list[tuple] = []
        self._swath_seen: set[tuple[int, int]] = set()
        # Robot-recorded path: midline between the field-12 rails (grid
        # coords, ordered along the path, deduped with the quads).
        self.rail_trail: list[tuple[float, float]] = []
        # Index into `trail` where the pose-history TAIL begins: everything
        # before it is superseded by the robot-recorded rail midline; the
        # tail (rails lag ~2 cells) is drawn from pose history as before
        # and replaced whenever new rail data arrives.
        self.rail_trail_split: int = 0
        # Ordered (append-only, deduped via _lidar_seen) so a map camera can
        # slice only the newly-added cells by index each frame instead of
        # scanning the whole set on the event loop.
        self.lidar_cells: list[tuple[int, int, int]] = []  # (cx, cy, value)
        self._lidar_seen: set[tuple[int, int]] = set()
        self._lidar_map_ts: int = 0
        # Map layer visibility flags (controlled by the layer switches,
        # restored by them on startup; default: everything visible)
        self.draw_trail: bool = True
        self.draw_cleaned_area: bool = True
        self.draw_furniture: bool = True
        self.draw_lidar_walls: bool = True
        self._last_display_map_resub: float = 0.0
        self._consecutive_failures = 0
        self._max_failures = 5  # 5 * 60s = 5 minutes before entities go unavailable

    async def async_setup(self) -> None:
        """Connect to the vacuum and start the WebSocket listener.

        Queries initial state BEFORE starting the listener to avoid
        concurrent recv issues (see 446be16). Each command is wrapped in
        try/except so setup never crashes if the robot is asleep.
        The listener's keepalive loop handles waking independently.
        """
        await self.client.connect()

        # Fetch initial state BEFORE starting listener (no concurrent recv)
        try:
            await self.client.get_device_info()
        except Exception:
            _LOGGER.debug("Could not fetch device info at startup")

        try:
            await self.client.get_status(full_update=True)
        except Exception:
            _LOGGER.debug("Could not fetch initial status")

        try:
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Could not fetch initial map")

        # Subscribe to broadcast topics (display_map, working_status, etc.)
        # Must be sent before listener starts so display_map flows during cleaning.
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Could not send topic subscription at startup")

        self.async_set_updated_data(self.client.state)

        # Set up push callback and start persistent listener
        self.client.on_state_update = self._on_state_update
        self._listen_task = self.config_entry.async_create_background_task(
            self.hass,
            self.client.start_listening(),
            f"{DOMAIN}_ws_listener",
        )

        state = self.client.state
        _LOGGER.info(
            "Narwal startup: status=%s, battery=%d, docked=%s, awake=%s",
            state.working_status.name, state.battery_level,
            state.is_docked, self.client.robot_awake,
        )

        # If robot didn't respond, use fast polling to catch it when it wakes
        if state.working_status == WorkingStatus.UNKNOWN:
            self._fast_poll_remaining = FAST_POLL_MAX
            self.update_interval = FAST_POLL_INTERVAL
            _LOGGER.info(
                "Robot asleep — fast polling every %ds until it responds",
                int(FAST_POLL_INTERVAL.total_seconds()),
            )

    def _on_state_update(self, state: NarwalState) -> None:
        """Handle a push state update from the WebSocket listener."""
        # Push data arriving means robot is reachable — reset failure counter
        self._consecutive_failures = 0

        # Fetch static map if missing (get_map failed at startup)
        if state.map_data is None and not self._map_fetch_pending:
            self._map_fetch_pending = True
            self.config_entry.async_create_background_task(
                self.hass,
                self._fetch_missing_map(),
                f"{DOMAIN}_map_fetch",
            )

        # Detect return-to-dock transition: CLEANING/CLEANING_ALT → docked state.
        # Broadcast dock fields are stale after docking — immediate poll
        # refreshes them so UI shows DOCKED instead of IDLE.
        # On older FW the transition is → STANDBY; on v01.07.23+ it may
        # go directly to DOCKED_V2(2).
        if (
            state.working_status in (
                WorkingStatus.STANDBY, WorkingStatus.DOCKED_V2,
            )
            and self._prev_working_status
            in (WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT)
        ):
            _LOGGER.info("Return-to-dock detected, refreshing dock status")
            self.hass.async_create_task(self._refresh_dock_status())
        self._prev_working_status = state.working_status

        # display_map dropout recovery: if cleaning but no display_map for
        # 30s, re-send topic subscription. Only subscription — no wake burst
        # (wake bursts during cleaning cause pause bouncing).
        # Point-navi tasks (fw v01.08.03+) keep working_status=DOCKED_V2
        # while driving; is_docked=False there means the robot is active,
        # so recovery must run for that shape too. Gating on the explicit
        # off-dock veto keeps a sleeping robot (dock fields absent) exempt.
        is_cleaning = state.working_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT,
        ) or (
            state.working_status == WorkingStatus.DOCKED_V2
            and not state.is_docked
        )
        if is_cleaning:
            display_age = self.client.last_display_map_age
            now = time.monotonic()
            if (
                display_age > 30.0
                and now - self._last_display_map_resub > 45.0
            ):
                _LOGGER.info(
                    "display_map dropout (%.0fs) — re-subscribing to topics",
                    display_age,
                )
                self._last_display_map_resub = now
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._resub_topics(),
                    f"{DOMAIN}_resub",
                )

        self._update_trail(state)

        self.async_set_updated_data(state)

        # Broadcast arrived — switch back to normal polling if in fast mode
        if self._fast_poll_remaining > 0:
            self._fast_poll_remaining = 0
            self.update_interval = POLL_INTERVAL
            _LOGGER.info(
                "Broadcast received (status=%s) — normal polling restored",
                state.working_status.name,
            )

    def _update_trail(self, state: NarwalState) -> None:
        """Record the robot position to the shared cleaning trail.

        Clears the trail when a new cleaning session starts. Uses a
        minimum-distance filter instead of a timer so the trail is dense
        while the robot moves and doesn't grow while it's parked.
        """
        # Session-transition detection (skip transient UNKNOWN so a
        # broadcast dropout doesn't fake a "new session" and wipe the trail)
        if state.working_status != WorkingStatus.UNKNOWN:
            is_cleaning = state.is_cleaning_session
            if is_cleaning and not self._was_cleaning_session:
                _LOGGER.info("New cleaning session — clearing trail/strip/non-carpet lidar")
                self.trail.clear()
                self._trail_last = None
                self.swath_quads.clear()
                self._swath_seen.clear()
                self.rail_trail.clear()
                self.rail_trail_split = 0
                # Drop non-carpet lidar hits (re-scanned this session); keep
                # carpet cells — they persist across sessions.
                self.lidar_cells[:] = [
                    c for c in self.lidar_cells if c[2] & LIDAR_CARPET_FLAG
                ]
                self._lidar_seen = {(c[0], c[1]) for c in self.lidar_cells}
            self._was_cleaning_session = is_cleaning

        self._update_map_layers(state)

        static_map = state.map_data
        if not static_map:
            return
        # Freshest pose from display_map OR the planned-trajectory head, so
        # the trail keeps growing through display_map dropouts.
        pose = state.best_robot_position()
        if pose is None:
            return
        grid_pos = (
            pose[0] - static_map.origin_x,
            pose[1] - static_map.origin_y,
        )
        if len(self.trail) >= TRAIL_MAX_POINTS:
            return
        last = self._trail_last
        if last is not None and math.hypot(
            grid_pos[0] - last[0], grid_pos[1] - last[1],
        ) < TRAIL_MIN_DIST:
            return
        self.trail.append(grid_pos)
        self._trail_last = grid_pos

    def _update_map_layers(self, state: NarwalState) -> None:
        """Accumulate the vacuumed strip (field 12) and lidar walls (field 7).

        Field 12 delivers a sliding window of the recent path as two
        parallel rails; consecutive rail-point pairs form quads that are
        deduped by their midpoint so overlapping windows don't re-add.
        Field 7 delivers incremental lidar wall/obstacle cell observations
        (row-major indexes, stride = map width); they persist across
        cleaning sessions and reset when the saved map changes.
        """
        display = state.map_display_data
        static_map = state.map_data
        if not display or not static_map or static_map.width <= 0:
            return
        ox, oy = static_map.origin_x, static_map.origin_y

        # Reset lidar accumulation when the saved map changes
        map_ts = static_map.created_at or 0
        if map_ts != self._lidar_map_ts:
            self.lidar_cells.clear()
            self._lidar_seen.clear()
            self._lidar_map_ts = map_ts

        if display.wall_cells and len(self.lidar_cells) < LIDAR_CELLS_MAX:
            w, h = static_map.width, static_map.height
            for idx, val in display.wall_cells:
                cx, cy = idx % w, idx // w
                if 0 <= cx < w and 0 <= cy < h:
                    cell = (cx, cy)
                    if cell not in self._lidar_seen:
                        self._lidar_seen.add(cell)
                        # Keep the per-cell value so the renderer can colour
                        # cells by classification (diagnostic: carpet vs wall).
                        self.lidar_cells.append((cx, cy, val))

        if len(display.rail_paths) == 2 and len(self.swath_quads) < SWATH_QUADS_MAX:
            r0, r1 = display.rail_paths
            n = min(len(r0), len(r1))
            rails_extended = False
            for i in range(n - 1):
                mid_x = (r0[i][0] + r1[i][0] + r0[i + 1][0] + r1[i + 1][0]) / 4
                mid_y = (r0[i][1] + r1[i][1] + r0[i + 1][1] + r1[i + 1][1]) / 4
                key = (round(mid_x * 4), round(mid_y * 4))
                if key in self._swath_seen:
                    continue
                self._swath_seen.add(key)
                self.swath_quads.append((
                    (r0[i][0] - ox, r0[i][1] - oy),
                    (r0[i + 1][0] - ox, r0[i + 1][1] - oy),
                    (r1[i + 1][0] - ox, r1[i + 1][1] - oy),
                    (r1[i][0] - ox, r1[i][1] - oy),
                ))
                # Midline polyline for the trail (robot's own path record)
                if len(self.rail_trail) < TRAIL_MAX_POINTS:
                    if not self.rail_trail:
                        self.rail_trail.append((
                            (r0[i][0] + r1[i][0]) / 2 - ox,
                            (r0[i][1] + r1[i][1]) / 2 - oy,
                        ))
                    self.rail_trail.append((
                        (r0[i + 1][0] + r1[i + 1][0]) / 2 - ox,
                        (r0[i + 1][1] + r1[i + 1][1]) / 2 - oy,
                    ))
                    rails_extended = True
            if rails_extended:
                # New rail data supersedes the pose-history prefix — the
                # rendered tail restarts from the current trail length
                # (this frame's pose is appended right after this call).
                self.rail_trail_split = len(self.trail)

    async def _fetch_missing_map(self) -> None:
        """Fetch static map when it's missing (get_map failed at startup)."""
        try:
            await self.client.get_map()
            _LOGGER.info("Static map loaded (was missing at startup)")
        except Exception:
            _LOGGER.debug("Map fetch failed — will retry on next broadcast")
            self._map_fetch_pending = False
            return
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Topic subscription failed after map load")
        self.async_set_updated_data(self.client.state)

    async def _resub_topics(self) -> None:
        """Re-send topic subscription to recover display_map during cleaning."""
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Topic re-subscription failed")

    async def _refresh_dock_status(self) -> None:
        """Immediate get_status() after return-to-dock to refresh dock fields."""
        try:
            await self.client.get_status(full_update=True)
            self.async_set_updated_data(self.client.state)
        except Exception:
            _LOGGER.debug("Failed to refresh dock status after transition")

    async def _async_update_data(self) -> NarwalState:
        """Polling fallback — fetch status if no push updates arrived.

        Reconnection is handled by the listener loop's exponential backoff.
        We do NOT call client.connect() here to avoid racing with the listener
        and violating the single-WS-connection-per-IP constraint.

        On poll failure, returns stale data for up to _max_failures consecutive
        failures (~5 minutes) before raising UpdateFailed.
        """
        try:
            if not self.client.connected:
                raise NarwalConnectionError("Not connected")
            await self.client.get_status(full_update=True)
        except Exception as err:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_failures:
                raise UpdateFailed(
                    f"Vacuum unreachable for {self._consecutive_failures} consecutive polls"
                ) from err
            _LOGGER.debug(
                "Poll %d/%d failed (robot may be asleep): %s",
                self._consecutive_failures, self._max_failures, err,
            )
            return self.client.state  # stale data keeps entities available
        else:
            self._consecutive_failures = 0

        # Retry map fetch if it failed during setup
        if self.client.state.map_data is None:
            try:
                await self.client.get_map()
            except Exception:
                pass

        # Manage fast poll countdown
        if self._fast_poll_remaining > 0:
            if self.client.state.working_status != WorkingStatus.UNKNOWN:
                self._fast_poll_remaining = 0
                self.update_interval = POLL_INTERVAL
            else:
                self._fast_poll_remaining -= 1
                if self._fast_poll_remaining <= 0:
                    self.update_interval = POLL_INTERVAL

        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        await super().async_shutdown()
