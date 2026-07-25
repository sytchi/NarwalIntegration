"""Map camera entity for Narwal vacuum — MJPEG streaming for live updates.

One camera is created: ``camera.*_map_hd`` — an upscaled render
(``map_scale`` option, default 4×) with anti-aliased overlays and real
``calibration_points`` attributes for card configs using
``calibration_source: {camera: true}``.

The legacy 1:1 ``camera.*_map`` entity was removed in 2.0.0.
"""

from __future__ import annotations

import io
import logging
import math
import time
from functools import partial

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import CONF_MAP_SCALE, DEFAULT_MAP_SCALE
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)

# Minimum seconds between re-renders while the map is being watched
# (display_map arrives every ~1.5s but PIL rendering is CPU-bound — no need
# to render every broadcast).
_MIN_RENDER_INTERVAL = 2

# Re-render interval when nobody is looking at the map. Snapshots/automations
# and the map chip still get a reasonably fresh frame, but we spend far less
# CPU redrawing a map no one is viewing during a long clean.
_IDLE_RENDER_INTERVAL = 10

# A still-image request within this many seconds means a viewer is active
# (the MJPEG streamer pulls a frame every _MIN_RENDER_INTERVAL while open).
_VIEWER_ACTIVE_WINDOW = 8

# Debug view: blank canvas with robot dot + trail.
# Set to False to use the real map renderer instead.
_DEBUG_VIEW = False
_DEBUG_CANVAS_SIZE = 600  # pixels


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal map camera entity."""
    coordinator = entry.runtime_data
    hd_scale = entry.options.get(CONF_MAP_SCALE, DEFAULT_MAP_SCALE)
    async_add_entities([NarwalMapCamera(coordinator, scale=hd_scale)])


class NarwalMapCamera(NarwalEntity, Camera):
    """Camera entity that streams the vacuum's map as MJPEG."""

    _attr_is_streaming = True
    # These change on every render (~2s while watched); keep them out of the
    # recorder so we don't write a history row + JSON blob to disk each time.
    _unrecorded_attributes = frozenset({"render_count", "calibration_points"})

    def __init__(
        self, coordinator: NarwalCoordinator, scale: int = 4,
    ) -> None:
        """Initialize the HD map camera entity.

        Args:
            scale: Base-map upscale factor (the ``map_scale`` option).
        """
        NarwalEntity.__init__(self, coordinator)
        Camera.__init__(self)
        device_id = coordinator.config_entry.data["device_id"]
        self._scale = max(1, int(scale))
        self._attr_name = "Map HD"
        self._attr_unique_id = f"{device_id}_map_hd"
        self._cached_image: bytes | None = None
        self._cache_key: tuple = ()
        self._last_render_time: float = 0.0
        self._render_count: int = 0
        # Cached base map (PIL Image) — only re-rendered when static map changes
        self._base_map_image = None  # PIL Image or None
        self._base_map_rgba = None  # RGBA copy for compositing (avoids per-frame convert)
        self._base_wall_mask = None  # set of (cx,cy) walls in the saved map
        self._base_map_ts: int = 0  # created_at of the static map used for base
        self._base_map_furniture: bool = True  # furniture flag used for base
        # Debug view state — growing viewport around the session trail
        self._dock_pos: tuple[float, float] | None = None
        self._vp_min_x: float = 0.0
        self._vp_max_x: float = 0.0
        self._vp_min_y: float = 0.0
        self._vp_max_y: float = 0.0
        self._vp_initialized: bool = False
        # Persistent accumulation layers: the vacuumed strip and lidar walls
        # only grow during a session, so we draw the new items onto these
        # layers each frame (O(new)) instead of re-drawing the whole set
        # (O(total)) — the latter blocks the render thread long enough late in
        # a long clean to starve the event loop. Rebuilt with the base map
        # (lidar colour samples the base) and reset on a new session.
        self._swath_layer = None  # PIL RGBA Image or None
        self._lidar_layer = None  # PIL RGBA Image or None
        self._lidar_mask = None  # PIL "L" binary mask grown with _lidar_layer
        self._swath_drawn: int = 0  # swath quads already on _swath_layer
        self._lidar_drawn: int = 0  # lidar cells already on _lidar_layer
        self._render_inflight: bool = False
        # Monotonic time of the last still-image request (MJPEG stream frame,
        # snapshot or dashboard fetch). Drives the adaptive render cadence:
        # while something is actively pulling frames we keep the map fresh;
        # when nobody is watching we re-render far less often.
        self._last_image_access: float = 0.0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None,
    ) -> bytes | None:
        """Return the latest map image as PNG (for snapshot/polling clients)."""
        # Mark a viewer as active so the coordinator keeps rendering at the
        # fast cadence; goes stale when nobody pulls frames.
        self._last_image_access = time.monotonic()
        return self._cached_image

    async def handle_async_mjpeg_stream(self, request):
        """Stream map as MJPEG using HA's built-in still-image streamer."""
        from homeassistant.components.camera import async_get_still_stream

        return await async_get_still_stream(
            request, self.async_camera_image, "image/png", _MIN_RENDER_INTERVAL,
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose render count and calibration_points.

        render_count changes on every render so HA detects state changes
        for MJPEG refresh. calibration_points let xiaomi-vacuum-map-card
        map image pixels to robot world coordinates
        (``calibration_source: {camera: true}``).
        """
        attrs: dict = {"render_count": self._render_count}
        static_map = self.coordinator.client.state.map_data
        if static_map and static_map.width > 0 and static_map.height > 0:
            from .narwal_client.map_renderer import compute_calibration_points

            attrs["calibration_points"] = compute_calibration_points(
                static_map.width,
                static_map.height,
                static_map.origin_x,
                static_map.origin_y,
                self._scale,
            )
        return attrs

    def _record_debug_viewport(self, x: float, y: float) -> None:
        """Track dock position and expand the debug viewport bounds."""
        if self._dock_pos is None:
            self._dock_pos = (x, y)

        if not self._vp_initialized:
            self._vp_min_x = x
            self._vp_max_x = x
            self._vp_min_y = y
            self._vp_max_y = y
            self._vp_initialized = True
        else:
            if x < self._vp_min_x:
                self._vp_min_x = x
            if x > self._vp_max_x:
                self._vp_max_x = x
            if y < self._vp_min_y:
                self._vp_min_y = y
            if y > self._vp_max_y:
                self._vp_max_y = y

    @callback
    def _handle_coordinator_update(self) -> None:
        """Re-render the map when new data arrives from the coordinator."""
        state = self.coordinator.client.state
        display = state.map_display_data

        if _DEBUG_VIEW:
            if not display or (display.robot_x == 0.0 and display.robot_y == 0.0):
                self.async_write_ha_state()
                return
            self._record_debug_viewport(display.robot_x, display.robot_y)
            new_key = (display.robot_x, display.robot_y, display.robot_heading)
        else:
            static_map = state.map_data
            if not static_map or not static_map.compressed_map:
                self.async_write_ha_state()
                return
            if static_map.width <= 0 or static_map.height <= 0:
                self.async_write_ha_state()
                return

            static_ts = static_map.created_at or 0
            trail_len = len(self.coordinator.trail)
            # Include the target zones so setting/clearing one forces a re-render
            # even when the robot is stationary on the dock.
            zones_key = tuple(state.active_zones)
            traj = state.planned_trajectory
            traj_key = (
                (len(traj.points), traj.points[0], traj.points[-1])
                if traj and traj.points else ()
            )
            co = self.coordinator
            layers_key = (
                len(co.swath_quads),
                len(co.lidar_cells),
                co.draw_trail,
                co.draw_cleaned_area,
                co.draw_furniture,
                co.draw_lidar_walls,
            )
            if display:
                new_key = (static_ts, display.robot_x, display.robot_y,
                           display.robot_heading, trail_len, zones_key,
                           traj_key, layers_key)
            else:
                new_key = (static_ts, trail_len, zones_key, traj_key,
                           layers_key)

        now = time.monotonic()
        since_render = now - self._last_render_time if self._last_render_time else 999

        # Nothing changed / throttled → no state to emit (the render path
        # writes state after it actually re-renders). Skipping these avoids
        # firing the event bus and rebuilding attrs on every broadcast.
        if new_key == self._cache_key and self._cached_image:
            return

        # Render fast while someone is watching (a still-image request came in
        # recently), otherwise fall back to the idle cadence.
        watching = (now - self._last_image_access) < _VIEWER_ACTIVE_WINDOW
        render_interval = _MIN_RENDER_INTERVAL if watching else _IDLE_RENDER_INTERVAL
        if self._cached_image and since_render < render_interval:
            return

        if self._render_inflight:
            # A render is already running; its persistent layers must not be
            # mutated by a second executor job. Skip — the next coordinator
            # update will render the newest state anyway.
            return
        self._render_inflight = True
        self.hass.async_create_task(self._async_render(display, new_key))

    async def _async_render(self, display, new_key) -> None:
        """Serialize renders so the persistent swath/lidar layers are never
        mutated by two executor jobs at once; always clear the in-flight
        guard when the render finishes (or fails)."""
        try:
            await self._render_impl(display, new_key)
        finally:
            self._render_inflight = False

    async def _render_impl(self, display, new_key) -> None:
        """Render the map image in an executor thread."""
        if _DEBUG_VIEW and display:
            trail = list(self.coordinator.trail)
            dock = self._dock_pos
            viewport = None
            if self._vp_initialized:
                viewport = (
                    self._vp_min_x, self._vp_min_y,
                    self._vp_max_x, self._vp_max_y,
                )
            try:
                png_bytes = await self.hass.async_add_executor_job(
                    _render_debug_view,
                    display.robot_x,
                    display.robot_y,
                    display.robot_heading,
                    trail,
                    dock,
                    viewport,
                )
                if png_bytes:
                    self._cached_image = png_bytes
                    self._cache_key = new_key
                    self._last_render_time = time.monotonic()
                    self._render_count += 1
            except Exception:
                _LOGGER.exception("Failed to render debug view")
            self.async_write_ha_state()
            return

        # --- Normal map render path (with cached base + overlay) ---
        state = self.coordinator.client.state
        static_map = state.map_data
        if not static_map:
            self.async_write_ha_state()
            return

        from .narwal_client.map_renderer import (
            decode_obstacle_mask,
            render_base_map,
            render_map_frame,
        )

        # Rebuild base map when static map data OR the furniture flag changes
        static_ts = static_map.created_at or 0
        draw_furniture = self.coordinator.draw_furniture
        if (
            self._base_map_image is None
            or static_ts != self._base_map_ts
            or draw_furniture != self._base_map_furniture
        ):
            room_names: dict[int, str] | None = None
            if static_map.rooms:
                room_names = {
                    r.room_id: r.display_name for r in static_map.rooms
                }
            base_img = await self.hass.async_add_executor_job(
                partial(
                    render_base_map,
                    static_map.compressed_map,
                    static_map.width,
                    static_map.height,
                    room_names=room_names,
                    obstacles=static_map.obstacles if draw_furniture else None,
                    origin_x=static_map.origin_x,
                    origin_y=static_map.origin_y,
                    scale=self._scale,
                )
            )
            if base_img:
                self._base_map_image = base_img
                # Cache the RGBA form once so the per-frame composite doesn't
                # redo the RGB→RGBA convert.
                self._base_map_rgba = base_img.convert("RGBA")
                # Saved-map wall cells, to tell lidar hits on real walls from
                # floating false positives.
                self._base_wall_mask = decode_obstacle_mask(
                    static_map.compressed_map,
                    static_map.width,
                    static_map.height,
                )
                self._base_map_ts = static_ts
                self._base_map_furniture = draw_furniture
                # The base changed (map, furniture or scale) → lidar cell
                # colours sample the base, so the accumulation layers are
                # stale. Drop them; they rebuild from the coordinator's full
                # sets on the next frame.
                self._swath_layer = None
                self._lidar_layer = None
                self._lidar_mask = None
                self._swath_drawn = 0
                self._lidar_drawn = 0
                _LOGGER.info(
                    "Base map rendered (ts=%d, %dx%d, scale=%d)",
                    static_ts, static_map.width, static_map.height, self._scale,
                )
            else:
                self.async_write_ha_state()
                return

        # Compute robot grid position from the freshest source (display_map
        # pose or planned-trajectory head — display_map drops out for 30s+
        # during cleaning while the trajectory keeps flowing).
        robot_x = None
        robot_y = None
        robot_heading = None
        pose = state.best_robot_position()
        if pose is not None:
            robot_x = pose[0] - static_map.origin_x
            robot_y = pose[1] - static_map.origin_y
            robot_heading = pose[2]
        if display:
            grid_pos = display.to_grid_coords(
                static_map.resolution, static_map.origin_x, static_map.origin_y,
            )
            if (
                grid_pos is not None
                and self._render_count % 30 == 0
                and _LOGGER.isEnabledFor(logging.DEBUG)
            ):
                # Log transform details periodically for debugging position offset
                # (the lookups below decode the whole map — gated on DEBUG so
                # they never run on the event loop in normal operation).
                try:
                    # Compare display_map dock ref (field 5) with static map dock
                    dock_ref_grid_x = dock_ref_grid_y = None
                    if display.dock_ref_x != 0.0 or display.dock_ref_y != 0.0:
                        dock_ref_grid_x = display.dock_ref_x - static_map.origin_x
                        dock_ref_grid_y = display.dock_ref_y - static_map.origin_y
                    # Room lookup at robot grid position
                    from .narwal_client.map_renderer import lookup_room_at_grid
                    robot_rid, robot_room = lookup_room_at_grid(
                        static_map.compressed_map, static_map.width, static_map.height,
                        int(grid_pos[0]), int(grid_pos[1]),
                    )
                    dock_rid, dock_room = (-1, "n/a")
                    if static_map.dock_x is not None and static_map.dock_y is not None:
                        dock_rid, dock_room = lookup_room_at_grid(
                            static_map.compressed_map, static_map.width, static_map.height,
                            int(static_map.dock_x), int(static_map.dock_y),
                        )
                    _LOGGER.debug(
                        "POSITION DIAG: robot_raw=(%.2f, %.2f) robot_grid=(%.1f, %.1f) robot_room=%s(id=%d) "
                        "| dock_ref_raw=(%.2f, %.2f) dock_ref_grid=(%.1f, %.1f) "
                        "| static_dock_grid=(%.1f, %.1f) dock_room=%s(id=%d) "
                        "| res=%d origin=(%d, %d) map=%dx%d",
                        display.robot_x, display.robot_y,
                        grid_pos[0], grid_pos[1], robot_room, robot_rid,
                        display.dock_ref_x, display.dock_ref_y,
                        dock_ref_grid_x or 0, dock_ref_grid_y or 0,
                        static_map.dock_x or 0, static_map.dock_y or 0,
                        dock_room, dock_rid,
                        static_map.resolution,
                        static_map.origin_x, static_map.origin_y,
                        static_map.width, static_map.height,
                    )
                except Exception:
                    _LOGGER.debug("POSITION DIAG failed", exc_info=True)

        # Trail line: the robot-recorded rail midline (field 12) then the
        # pose-history tail. It's composed inside render_map_frame (the
        # executor) so copying up to TRAIL_MAX_POINTS points each frame stays
        # off the event loop — here we only capture references + the split.
        rail_trail = self.coordinator.rail_trail
        coord_trail = self.coordinator.trail
        rail_trail_split = self.coordinator.rail_trail_split
        # active_zones are in robot WORLD coords; the renderer draws in grid
        # coords (like the robot/trail), so subtract the map origin here.
        zones = None
        if state.active_zones:
            ox, oy = static_map.origin_x, static_map.origin_y
            zones = [
                (x1 - ox, y1 - oy, x2 - ox, y2 - oy)
                for (x1, y1, x2, y2) in state.active_zones
            ]

        # Planned trajectory (world coords → grid, only while cleaning).
        # Filter out the docking-maneuver anomaly frames: ~100 identical
        # points far outside the map bbox.
        planned = None
        traj = state.planned_trajectory
        if traj and traj.points and state.is_cleaning_session:
            ox, oy = static_map.origin_x, static_map.origin_y
            pts = [
                (px - ox, py - oy)
                for (px, py) in traj.points
            ]
            pts = [
                p for p in pts
                if 0 <= p[0] < static_map.width and 0 <= p[1] < static_map.height
            ]
            if len(set(pts)) >= 2:
                planned = pts

        co = self.coordinator
        # A new cleaning session clears the coordinator's accumulators; when
        # they shrink below what we've drawn, drop our layers so they don't
        # keep last session's marks.
        if (
            len(co.swath_quads) < self._swath_drawn
            or len(co.lidar_cells) < self._lidar_drawn
        ):
            self._swath_layer = None
            self._lidar_layer = None
            self._lidar_mask = None
            self._swath_drawn = 0
            self._lidar_drawn = 0

        # Snapshot lengths on the event loop and slice only the new items
        # (both accumulators are ordered + append-only). Advance the trackers
        # exactly to what we drew — the coordinator may append more during the
        # executor render; those are picked up next frame.
        swath_total = len(co.swath_quads)
        lidar_total = len(co.lidar_cells)
        new_swath = list(co.swath_quads[self._swath_drawn:swath_total])
        new_cells = list(co.lidar_cells[self._lidar_drawn:lidar_total])

        overlay_kwargs = {
            "robot_x": robot_x,
            "robot_y": robot_y,
            "robot_heading": robot_heading,
            "zones": zones,
            "dock_x": static_map.dock_x,
            "dock_y": static_map.dock_y,
            "planned_path": planned,
            "show_trail_line": co.draw_trail,
        }
        try:
            png_bytes, swath_layer, lidar_layer, lidar_mask = (
                await self.hass.async_add_executor_job(
                    partial(
                        render_map_frame,
                        self._base_map_image,
                        static_map.width,
                        static_map.height,
                        scale=self._scale,
                        swath_layer=self._swath_layer,
                        new_swath_quads=new_swath,
                        show_swath=co.draw_cleaned_area,
                        lidar_layer=self._lidar_layer,
                        lidar_mask=self._lidar_mask,
                        new_wall_cells=new_cells,
                        show_lidar=co.draw_lidar_walls,
                        overlay_kwargs=overlay_kwargs,
                        base_rgba=self._base_map_rgba,
                        rail_trail=rail_trail,
                        coord_trail=coord_trail,
                        rail_trail_split=rail_trail_split,
                        wall_cells_set=self._base_wall_mask,
                    )
                )
            )
            # Persist the (possibly newly created) layers and advance trackers.
            self._swath_layer = swath_layer
            self._lidar_layer = lidar_layer
            self._lidar_mask = lidar_mask
            self._swath_drawn = swath_total
            self._lidar_drawn = lidar_total

            if png_bytes:
                self._cached_image = png_bytes
                self._cache_key = new_key
                self._last_render_time = time.monotonic()
                self._render_count += 1

        except Exception:
            _LOGGER.exception("Failed to render map overlay")

        self.async_write_ha_state()


def _render_debug_view(
    robot_x: float,
    robot_y: float,
    robot_heading: float,
    trail: list[tuple[float, float]],
    dock_pos: tuple[float, float] | None = None,
    viewport: tuple[float, float, float, float] | None = None,
) -> bytes:
    """Render a blank canvas with full cleaning trail, dock marker, and robot dot."""
    from PIL import Image, ImageDraw, ImageFont

    size = _DEBUG_CANVAS_SIZE
    img = Image.new("RGB", (size, size), (20, 20, 30))
    draw = ImageDraw.Draw(img)

    if viewport:
        min_x, min_y, max_x, max_y = viewport
    elif trail:
        all_x = [p[0] for p in trail]
        all_y = [p[1] for p in trail]
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
    else:
        min_x = robot_x - 250
        max_x = robot_x + 250
        min_y = robot_y - 250
        max_y = robot_y + 250

    padding = 100
    range_x = max(max_x - min_x, 200) + padding * 2
    range_y = max(max_y - min_y, 200) + padding * 2
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2

    margin = 50
    usable = size - margin * 2
    scale = usable / max(range_x, range_y)

    def to_px(cx: float, cy: float) -> tuple[int, int]:
        px = int((cx - center_x) * scale + size / 2)
        py = int(-(cy - center_y) * scale + size / 2)
        return px, py

    grid_interval = 100
    grid_start_x = int(center_x - range_x / 2)
    grid_start_x = grid_start_x - (grid_start_x % grid_interval)
    grid_start_y = int(center_y - range_y / 2)
    grid_start_y = grid_start_y - (grid_start_y % grid_interval)

    grid_color = (35, 35, 45)
    for gx in range(grid_start_x, int(center_x + range_x / 2) + grid_interval, grid_interval):
        px, _ = to_px(gx, 0)
        if 0 <= px < size:
            draw.line([(px, 0), (px, size)], fill=grid_color)
    for gy in range(grid_start_y, int(center_y + range_y / 2) + grid_interval, grid_interval):
        _, py = to_px(0, gy)
        if 0 <= py < size:
            draw.line([(0, py), (size, py)], fill=grid_color)

    if trail:
        n = len(trail)
        if n > 3000:
            step = max(n // 2000, 2)
            bulk = trail[: n - 200 : step]
            recent = trail[n - 200 :]
            render_trail = bulk + recent
        else:
            render_trail = trail

        recent_start = max(len(render_trail) - 200, 0)
        for i in range(len(render_trail) - 1):
            if i >= recent_start:
                color = (30, 120, 255)
            else:
                color = (15, 60, 130)
            x1, y1 = to_px(render_trail[i][0], render_trail[i][1])
            x2, y2 = to_px(render_trail[i + 1][0], render_trail[i + 1][1])
            draw.line([(x1, y1), (x2, y2)], fill=color, width=2)

    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    if dock_pos:
        dx, dy = to_px(dock_pos[0], dock_pos[1])
        r = 8
        draw.ellipse(
            [dx - r, dy - r, dx + r, dy + r],
            fill=(255, 140, 0),
            outline=(255, 200, 100),
        )
        draw.text((dx + 12, dy - 6), "DOCK", fill=(255, 140, 0), font=font)

    rx, ry = to_px(robot_x, robot_y)
    dot_r = 7
    draw.ellipse(
        [rx - dot_r, ry - dot_r, rx + dot_r, ry + dot_r],
        fill=(0, 255, 80),
        outline=(150, 255, 180),
    )

    heading_rad = math.radians(robot_heading)
    hx = rx + int(18 * math.cos(heading_rad))
    hy = ry - int(18 * math.sin(heading_rad))
    draw.line([(rx, ry), (hx, hy)], fill=(0, 255, 80), width=2)

    text_color = (180, 180, 190)
    dim_color = (100, 100, 120)
    y_text = 5
    draw.text((5, y_text), f"pos: ({robot_x:.1f}, {robot_y:.1f}) cm", fill=text_color, font=font)
    y_text += 15
    draw.text((5, y_text), f"trail: {len(trail)} pts", fill=dim_color, font=font)
    y_text += 15
    draw.text((5, y_text), f"heading: {robot_heading:.0f}", fill=dim_color, font=font)
    y_text += 15
    view_w = range_x / 100
    view_h = range_y / 100
    draw.text((5, y_text), f"view: {view_w:.1f}x{view_h:.1f}m", fill=dim_color, font=font)

    ox, oy = to_px(0, 0)
    if 0 <= ox < size and 0 <= oy < size:
        draw.line([(ox - 8, oy), (ox + 8, oy)], fill=(80, 80, 80))
        draw.line([(ox, oy - 8), (ox, oy + 8)], fill=(80, 80, 80))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
