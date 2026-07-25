"""Map renderer for Narwal vacuum — converts raw map data to PNG bytes.

Pure Python module with no Home Assistant dependencies.
Uses Pillow for image rendering.

Map data format (confirmed from live robot data):
  - Compressed with standard zlib (header 78 01)
  - Decompressed data is a protobuf message: field 1 = packed repeated varints
  - Skip 4-byte protobuf header, then decode varints
  - Each varint encodes: room_id = value >> 8, pixel_type = value & 0xFF
  - Value 0 = unknown/outside, 0x20 = unassigned floor, 0x28 = unassigned obstacle
  - pixel_type & 0x10 = wall/border edge (darken the room color)

High-resolution rendering technique (NEAREST-upscaled base grid + a
supersampled RGBA vector layer downsampled with BOX for anti-aliasing)
adapted from Piotr Machowski's Xiaomi Cloud Map Extractor (MIT):
https://github.com/PiotrMachowski/Home-Assistant-custom-components-Xiaomi-Cloud-Map-Extractor
"""

from __future__ import annotations

import io
import logging
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image, ImageDraw

_LOGGER = logging.getLogger(__name__)

# Room color palette (RGB) — up to 22 rooms
ROOM_COLORS: list[tuple[int, int, int]] = [
    (100, 149, 237),  # 1 - cornflower blue
    (144, 238, 144),  # 2 - light green
    (255, 182, 193),  # 3 - light pink
    (255, 218, 185),  # 4 - peach
    (221, 160, 221),  # 5 - plum
    (176, 224, 230),  # 6 - powder blue
    (255, 255, 150),  # 7 - light yellow
    (188, 143, 143),  # 8 - rosy brown
    (152, 251, 152),  # 9 - pale green
    (135, 206, 250),  # 10 - light sky blue
    (240, 128, 128),  # 11 - light coral
    (216, 191, 216),  # 12 - thistle
    (250, 250, 210),  # 13 - light goldenrod
    (173, 216, 230),  # 14 - light blue
    (244, 164, 96),   # 15 - sandy brown
    (245, 222, 179),  # 16 - wheat
    (127, 255, 212),  # 17 - aquamarine
    (255, 160, 122),  # 18 - light salmon
    (186, 218, 160),  # 19 - light green 2
    (255, 228, 196),  # 20 - bisque
    (200, 162, 200),  # 21 - light purple
    (174, 198, 207),  # 22 - pastel blue
]

# Obstacle/furniture annotation colors by catalog from APK map_furniture.json
OBSTACLE_COLORS: dict[int, tuple[int, int, int]] = {
    # Beds (1-3)
    1: (180, 140, 100),    # single bed - tan
    2: (180, 140, 100),    # double bed - tan
    3: (180, 140, 100),    # baby bed - tan
    # Tables (4-7, 31)
    4: (160, 130, 90),     # dining table - brown
    5: (160, 130, 90),     # round table - brown
    6: (160, 130, 90),     # tea table - brown
    7: (160, 130, 90),     # round tea table - brown
    31: (160, 130, 90),    # desk - brown
    # Cupboards/storage (8-12)
    8: (140, 120, 100),    # TV stand - dark tan
    9: (140, 120, 100),    # bedside table - dark tan
    10: (140, 120, 100),   # locker - dark tan
    11: (140, 120, 100),   # wardrobe - dark tan
    12: (140, 120, 100),   # shoe cabinet - dark tan
    # Sofas/chairs (13-18, 30)
    13: (100, 160, 130),   # armchair - sage
    14: (100, 160, 130),   # sofa - sage
    15: (100, 160, 130),   # L-shaped sofa - sage
    16: (100, 160, 130),   # lazy chair - sage
    17: (100, 160, 130),   # chair - sage
    18: (100, 160, 130),   # bar chair - sage
    30: (100, 160, 130),   # U-shaped sofa - sage
    # Pets (19-21, 75-76)
    19: (200, 160, 120),   # cat toilet - peach
    20: (200, 160, 120),   # pet feeder - peach
    21: (200, 160, 120),   # pet house - peach
    75: (200, 160, 120),   # cat house - peach
    76: (200, 160, 120),   # dog house - peach
    # Appliances (22-25, 34)
    22: (150, 180, 200),   # washing machine - steel blue
    23: (150, 180, 200),   # refrigerator - steel blue
    24: (150, 180, 200),   # air conditioner - steel blue
    25: (150, 180, 200),   # fan - steel blue
    34: (150, 180, 200),   # stove - steel blue
    # Bathroom (28, 33)
    28: (120, 180, 220),   # toilet - light blue
    33: (120, 180, 220),   # washbasin - light blue
    # Misc (26-27, 29, 32, 77-78)
    26: (100, 180, 100),   # potted plant - green
    27: (200, 200, 220),   # floor mirror - silver
    29: (80, 80, 80),      # piano - dark gray
    32: (80, 80, 80),      # grand piano - dark gray
    77: (200, 200, 200),   # round placeholder - gray
    78: (200, 200, 200),   # weighing scale - gray
}
OBSTACLE_COLOR_DEFAULT = (200, 200, 200)

# Special pixel colors
COLOR_UNKNOWN = (40, 40, 40)         # outside map / unmapped
COLOR_UNASSIGNED_FLOOR = (200, 200, 200)  # floor not assigned to a room
COLOR_UNASSIGNED_OBSTACLE = (80, 80, 80)  # obstacle not in a room
COLOR_FALLBACK = (180, 180, 180)     # unknown room ID

# High-resolution rendering defaults
DEFAULT_SCALE = 4          # base image upscale factor (NEAREST)
OVERLAY_SUPERSAMPLE = 2    # extra supersample for the AA vector layer (BOX)
TRAIL_WIDTH = 0.8          # trail line width in grid cells
TRAIL_RECENT_SEGMENTS = 200  # segments drawn in the bright "recent" color
TRAIL_MAX_RENDER_POINTS = 2200  # decimation cap: bulk subsample + recent tail

# Overlay colors
COLOR_TRAIL_RECENT = (30, 120, 255, 255)
COLOR_TRAIL_OLD = (15, 60, 130, 255)
COLOR_TRAIL_STRIP = (225, 245, 255, 170)  # vacuumed strip (field 12 rails) — bright, semi-transparent
LIDAR_DARKEN = 100  # walls darken the floor by 80; lidar marks a bit more
COLOR_LIDAR_WALL = (0, 0, 0, 215)  # only the alpha is used (color from base)
COLOR_PLANNED_PATH = (255, 255, 255, 140)
COLOR_ZONE_FILL = (255, 170, 40, 70)
COLOR_ZONE_OUTLINE = (255, 150, 0, 255)
COLOR_CLEANED_TINT = (255, 255, 255, 45)
OBSTACLE_FILL_ALPHA = 40  # translucent furniture interior fill


def _load_font(size: int):
    """Load a truetype font at the given size with graceful fallbacks.

    Tries the bundled DejaVuSans first (full Latin coverage incl. Polish
    diacritics — the HA container ships no system truetype fonts and
    Pillow's sized load_default() falls back to Aileron, which renders
    e.g. "ó" as tofu). Older Pillow load_default() takes no size argument.
    """
    import os

    from PIL import ImageFont

    bundled = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
    for name in (bundled, "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def decompress_map(compressed: bytes) -> bytes:
    """Decompress map grid data using zlib.

    Args:
        compressed: Raw compressed bytes from the robot (zlib format, header 78 01).

    Returns:
        Decompressed bytes containing protobuf-wrapped pixel varints.
    """
    if not compressed:
        return b""

    # Try zlib auto-detect (wbits=47 handles zlib, gzip, and raw)
    try:
        return zlib.decompress(compressed, 47)
    except zlib.error:
        pass

    # Try zlib default
    try:
        return zlib.decompress(compressed)
    except zlib.error:
        pass

    # Try raw deflate
    try:
        return zlib.decompress(compressed, -15)
    except zlib.error:
        pass

    _LOGGER.warning(
        "Could not decompress map data (%d bytes), using raw", len(compressed)
    )
    return compressed


def _decode_packed_varints(data: bytes) -> list[int]:
    """Decode protobuf packed repeated varint field from decompressed map data.

    The decompressed data starts with a protobuf field header:
      byte 0: 0x0a (field 1, wire type 2 = length-delimited)
      bytes 1-3: varint length of the packed data

    After the header, the remaining bytes are packed varint pixel values.

    Args:
        data: Decompressed bytes from decompress_map().

    Returns:
        List of integer pixel values.
    """
    if len(data) < 4:
        return []

    # Skip protobuf header: field tag (1 byte) + length varint (variable)
    pos = 0
    if data[0] == 0x0A:  # field 1, wire type 2
        pos = 1
        # Skip the length varint
        while pos < len(data) and data[pos] & 0x80:
            pos += 1
        pos += 1  # skip the final byte of the length varint
    # else: try decoding from the start (no header)

    pixels: list[int] = []
    while pos < len(data):
        val = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        pixels.append(val)

    return pixels


def lookup_room_at_grid(
    compressed: bytes,
    width: int,
    height: int,
    grid_x: float,
    grid_y: float,
) -> tuple[int, str]:
    """Look up the room_id at a grid pixel coordinate.

    Returns (room_id, description) where description is one of:
      "room_N" for a valid room, "(empty)" for val=0,
      "(unassigned)" for 0x20/0x28, "(out_of_bounds)" if off grid.
    """
    px = int(grid_x)
    py = int(grid_y)
    if px < 0 or px >= width or py < 0 or py >= height:
        return (-1, f"(out_of_bounds: {px},{py} vs {width}x{height})")

    decompressed = decompress_map(compressed)
    if not decompressed:
        return (-1, "(no_data)")
    pixels = _decode_packed_varints(decompressed)

    idx = py * width + px
    if idx >= len(pixels):
        return (-1, f"(idx_overflow: {idx} >= {len(pixels)})")

    val = pixels[idx]
    if val == 0:
        return (0, "(empty)")
    if val in (0x20, 0x28):
        return (0, "(unassigned)")
    room_id = val >> 8
    ptype = val & 0xFF
    wall = " wall" if ptype & 0x10 else ""
    return (room_id, f"room_{room_id}{wall}")


def _darken(color: tuple[int, int, int], amount: int = 80) -> tuple[int, int, int]:
    """Darken an RGB color by subtracting from each channel."""
    return (
        max(0, color[0] - amount),
        max(0, color[1] - amount),
        max(0, color[2] - amount),
    )


def _draw_dock(
    draw: ImageDraw.ImageDraw,
    dock_x: int,
    dock_y: int,
    size: int = 6,
) -> None:
    """Draw a dock/charging station icon at the given grid coordinates.

    Renders as a small white filled circle (matching the Narwal app style).
    """
    radius = size // 2
    draw.ellipse(
        [dock_x - radius, dock_y - radius, dock_x + radius, dock_y + radius],
        fill=(255, 255, 255),
        outline=(180, 180, 180),
    )


def _draw_robot(
    draw: ImageDraw.ImageDraw,
    rx: int,
    ry: int,
    heading: float | None,
    radius: int,
) -> None:
    """Draw robot position with optional heading arrow.

    Args:
        draw: PIL ImageDraw instance.
        rx: Robot X in image coordinates (already Y-flipped).
        ry: Robot Y in image coordinates (already Y-flipped).
        heading: Heading in degrees (0=right, 90=up in world coords).
            None to draw circle only without heading arrow.
        radius: Circle radius in pixels.
    """
    import math

    # Blue filled circle with white outline
    draw.ellipse(
        [rx - radius, ry - radius, rx + radius, ry + radius],
        fill=(0, 120, 255),
        outline=(255, 255, 255),
    )

    # Heading arrow — white line from center in heading direction
    if heading is not None:
        # Convert degrees to radians. Heading 0=right, 90=up in world coords.
        # Image Y is flipped (down = positive), so negate the Y component.
        rad = math.radians(heading)
        arrow_len = radius * 2.5
        dx = math.cos(rad) * arrow_len
        dy = -math.sin(rad) * arrow_len  # negate for image Y-down
        draw.line(
            [(rx, ry), (rx + dx, ry + dy)],
            fill=(255, 255, 255),
            width=2,
        )


def render_map_png(
    decompressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
) -> bytes:
    """Render decompressed map data as a PNG image.

    Decodes the protobuf-packed varint pixel data and renders each pixel:
      - Value 0: unknown/outside (dark gray)
      - Value 0x20: unassigned floor (light gray)
      - Value 0x28: unassigned obstacle (dark gray)
      - Otherwise: room_id = value >> 8, pixel_type = value & 0xFF
        - pixel_type & 0x10: wall/border (darker shade of room color)
        - else: floor (room color)

    Args:
        decompressed: Decompressed map bytes (from decompress_map).
        width: Map width in pixels.
        height: Map height in pixels.
        robot_x: Robot X position in grid coordinates (optional).
        robot_y: Robot Y position in grid coordinates (optional).
        robot_heading: Robot heading in degrees (optional).
        dock_x: Dock X position in grid coordinates (optional).
        dock_y: Dock Y position in grid coordinates (optional).
        room_names: Mapping of room_id to display name (optional).

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    if not decompressed or width <= 0 or height <= 0:
        return b""

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        _LOGGER.error("Pillow is required for map rendering")
        return b""

    pixels = _decode_packed_varints(decompressed)
    expected = width * height

    if len(pixels) < expected:
        _LOGGER.warning(
            "Map has %d pixels, expected %d (%dx%d) — padding",
            len(pixels), expected, width, height,
        )
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    img = Image.new("RGB", (width, height), COLOR_UNKNOWN)
    px = img.load()

    # Track room pixel sums for centroid computation
    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}

    for i, val in enumerate(pixels):
        x = i % width
        y = i // width

        if val == 0:
            continue  # already set to COLOR_UNKNOWN
        elif val == 0x20:
            px[x, y] = COLOR_UNASSIGNED_FLOOR
        elif val == 0x28:
            px[x, y] = COLOR_UNASSIGNED_OBSTACLE
        else:
            room_id = val >> 8
            ptype = val & 0xFF

            if 1 <= room_id <= len(ROOM_COLORS):
                base = ROOM_COLORS[room_id - 1]
            else:
                base = COLOR_FALLBACK

            if ptype & 0x10:  # wall/border edge
                px[x, y] = _darken(base)
            else:
                px[x, y] = base

            # Accumulate for centroid (floor pixels only, not walls)
            if room_names and room_id in room_names and not (ptype & 0x10):
                room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
                room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
                room_count[room_id] = room_count.get(room_id, 0) + 1

    # Flip vertically BEFORE drawing overlays — pixel data is stored with
    # Y increasing upward (math coordinates) but images render Y downward.
    # Overlays (labels, dock, robot) use flipped coordinates so text is right-side up.
    img = img.transpose(Image.FLIP_TOP_BOTTOM)

    draw = ImageDraw.Draw(img)

    # Draw room labels at flipped centroids
    if room_names:
        try:
            font = ImageFont.truetype("arial.ttf", 10)
        except OSError:
            font = ImageFont.load_default()
        for rid, name in room_names.items():
            if not name or rid not in room_count:
                continue
            cx = room_sum_x[rid] // room_count[rid]
            cy = height - 1 - (room_sum_y[rid] // room_count[rid])
            bbox = font.getbbox(name)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            tx = cx - tw // 2
            ty = cy - th // 2
            # Dark outline for readability
            for ox, oy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                draw.text((tx + ox, ty + oy), name, fill=(0, 0, 0), font=font)
            draw.text((tx, ty), name, fill=(255, 255, 255), font=font)

    # Draw dock position (before robot so robot draws on top)
    # Flip dock Y to match the flipped image
    if dock_x is not None and dock_y is not None:
        dock_size = max(4, min(width, height) // 60)
        _draw_dock(draw, int(dock_x), height - 1 - int(dock_y), dock_size)

    # Draw robot position (flip Y) — skip if out of bounds
    if robot_x is not None and robot_y is not None:
        rx = int(robot_x)
        ry = height - 1 - int(robot_y)
        if 0 <= rx < width and 0 <= ry < height:
            radius = max(3, min(width, height) // 80)
            _draw_robot(draw, rx, ry, robot_heading, radius)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_base_map(
    compressed: bytes,
    width: int,
    height: int,
    room_names: dict[int, str] | None = None,
    obstacles: list | None = None,
    origin_x: int = 0,
    origin_y: int = 0,
    scale: int = 1,
) -> Image.Image | None:
    """Render the static floor plan as a PIL Image (no robot/dock overlay).

    Returns a PIL Image that can be cached and reused across frames.
    Only needs to be re-rendered when the static map data changes.

    The pixel grid is drawn at native resolution and upscaled with NEAREST
    so room edges stay crisp; labels are drawn after upscaling with a
    scaled font so text is smooth at any scale. The dock marker is drawn
    by render_overlay (vector layer) for anti-aliased edges.

    Args:
        obstacles: List of ObstacleInfo objects to render (optional).
        origin_x: Map origin X offset for obstacle coordinate transform.
        origin_y: Map origin Y offset for obstacle coordinate transform.
        scale: Integer upscale factor (1 = native grid resolution).
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        _LOGGER.error("Pillow is required for map rendering")
        return None

    decompressed = decompress_map(compressed)
    if not decompressed or width <= 0 or height <= 0:
        return None

    pixels = _decode_packed_varints(decompressed)
    expected = width * height

    if len(pixels) < expected:
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    img = Image.new("RGB", (width, height), COLOR_UNKNOWN)
    px = img.load()

    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}

    for i, val in enumerate(pixels):
        x = i % width
        y = i // width

        if val == 0:
            continue
        elif val == 0x20:
            px[x, y] = COLOR_UNASSIGNED_FLOOR
        elif val == 0x28:
            px[x, y] = COLOR_UNASSIGNED_OBSTACLE
        else:
            room_id = val >> 8
            ptype = val & 0xFF

            if 1 <= room_id <= len(ROOM_COLORS):
                base = ROOM_COLORS[room_id - 1]
            else:
                base = COLOR_FALLBACK

            if ptype & 0x10:
                px[x, y] = _darken(base)
            else:
                px[x, y] = base

            if room_names and room_id in room_names and not (ptype & 0x10):
                room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
                room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
                room_count[room_id] = room_count.get(room_id, 0) + 1

    img = img.transpose(Image.FLIP_TOP_BOTTOM)

    # Upscale the raw grid with NEAREST — keeps room edges crisp/blocky
    # instead of blurring them (Machowski technique).
    if scale > 1:
        img = img.resize((width * scale, height * scale), Image.NEAREST)

    draw = ImageDraw.Draw(img)
    stroke = max(1, scale // 2)

    # Label layout: room names get priority (drawn last, on top); obstacle
    # labels are smaller, sit BELOW their rectangle and are nudged further
    # down when they would collide with an already-placed label.
    placed_labels: list[tuple[float, float, float, float]] = []

    def _intersects(a: tuple, b: tuple) -> bool:
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    room_label_specs: list[tuple[float, float, str]] = []
    font = None
    if room_names:
        font = _load_font(10 * scale)
        for rid, name in room_names.items():
            if not name or rid not in room_count:
                continue
            cx = (room_sum_x[rid] / room_count[rid] + 0.5) * scale
            cy = (height - 0.5 - room_sum_y[rid] / room_count[rid]) * scale
            bbox = font.getbbox(name)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            tx = cx - tw / 2
            ty = cy - th / 2
            room_label_specs.append((tx, ty, name))
            placed_labels.append((tx, ty, tx + tw, ty + th))

    # Draw obstacle/furniture annotations (static data from get_map field 2.32)
    if obstacles:
        import math

        obs_font = _load_font(6 * scale)
        obs_stroke = max(1, scale // 3)
        drawable_obs = []
        for obs in obstacles:
            gx, gy = obs.to_grid_coords(origin_x, origin_y)
            # Skip out-of-bounds obstacles
            if gx < 0 or gx >= width or gy < 0 or gy >= height:
                continue
            # Rotated rectangle from the robot's float center/size/angle
            # (the angle was previously ignored — axis-aligned rects only)
            hw = max(1.0, obs.width / 2)
            hh = max(1.0, obs.height / 2)
            rad = math.radians(getattr(obs, "angle", 0.0) or 0.0)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            corners_img = [
                (
                    (gx + dx * cos_a - dy * sin_a + 0.5) * scale,
                    (height - 0.5 - (gy + dx * sin_a + dy * cos_a)) * scale,
                )
                for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
            ]
            color = OBSTACLE_COLORS.get(obs.type_id, OBSTACLE_COLOR_DEFAULT)
            drawable_obs.append((obs, gx, corners_img, color))

        # Translucent interior fill on an RGBA overlay (composited once so
        # overlapping furniture doesn't stack opacity against the floor)
        if drawable_obs:
            fill_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            fdraw = ImageDraw.Draw(fill_layer)
            for _obs, _gx, corners_img, color in drawable_obs:
                fdraw.polygon(corners_img, fill=(*color, OBSTACLE_FILL_ALPHA))
            img = Image.alpha_composite(
                img.convert("RGBA"), fill_layer,
            ).convert("RGB")
            draw = ImageDraw.Draw(img)

        for obs, gx, corners_img, color in drawable_obs:
            draw.line(
                [*corners_img, corners_img[0]], fill=color, width=obs_stroke,
            )
            # Label centered BELOW the rectangle, in a smaller font
            label = obs.display_name
            bbox = obs_font.getbbox(label)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            img_x = (gx + 0.5) * scale
            bottom = max(c[1] for c in corners_img)
            lx = img_x - tw / 2
            ly = bottom + scale
            step = th + scale
            for _ in range(6):
                box = (lx, ly, lx + tw, ly + th)
                if not any(_intersects(box, p) for p in placed_labels):
                    break
                ly += step
            placed_labels.append((lx, ly, lx + tw, ly + th))
            draw.text(
                (lx, ly), label, fill=color, font=obs_font,
                stroke_width=obs_stroke, stroke_fill=(0, 0, 0),
            )

    # Room names on top
    for tx, ty, name in room_label_specs:
        draw.text(
            (tx, ty), name, fill=(255, 255, 255), font=font,
            stroke_width=stroke, stroke_fill=(0, 0, 0),
        )

    return img


def _decimate_trail(
    trail: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Cap the number of rendered trail points.

    Subsamples the bulk of a long trail but always keeps the recent tail
    at full fidelity (same scheme as the debug view).
    """
    n = len(trail)
    if n <= TRAIL_MAX_RENDER_POINTS:
        return trail
    tail = TRAIL_RECENT_SEGMENTS
    bulk_budget = TRAIL_MAX_RENDER_POINTS - tail
    step = max(-(-(n - tail) // bulk_budget), 2)  # ceil division
    return trail[: n - tail : step] + trail[n - tail :]


def extend_swath_layer(
    layer: Image.Image | None,
    grid_width: int,
    grid_height: int,
    scale: int,
    new_quads: list[tuple] | None,
    supersample: int = OVERLAY_SUPERSAMPLE,
) -> Image.Image:
    """Draw only NEW vacuumed-strip quads onto a persistent supersample layer.

    The vacuumed strip and lidar observations only ever GROW during a cleaning
    session (they are reset when the robot starts a new one), so redrawing the
    full accumulated set every frame is O(total) and, late in a long clean,
    blocks the render thread long enough to starve the event loop. Keeping the
    drawn pixels on a persistent RGBA layer and appending only the new quads
    makes each frame O(new). Output is identical to drawing all quads on a
    fresh layer: the fill is a constant colour and ImageDraw REPLACES pixels,
    so re-drawing overlaps would be a no-op anyway.
    """
    from PIL import Image, ImageDraw

    s = scale * max(1, supersample)
    if layer is None:
        layer = Image.new("RGBA", (grid_width * s, grid_height * s), (0, 0, 0, 0))
    if new_quads:
        draw = ImageDraw.Draw(layer)

        def to_img(gx: float, gy: float) -> tuple[float, float]:
            return ((gx + 0.5) * s, (grid_height - 0.5 - gy) * s)

        for quad in new_quads:
            draw.polygon(
                [to_img(px, py) for px, py in quad], fill=COLOR_TRAIL_STRIP,
            )
    return layer


# Field-7 lidar cell value (field 7.2) is a bitfield. Reverse-engineered vs the
# saved map + user observations: bit2 of the low byte marks CARPET (values
# 0x05/0x07 — the robot's lidar sees the carpet edge as a height change, not a
# hard obstacle). Everything else is a raw lidar hit: reliable where it lines up
# with a saved-map wall, noisy (false positives) where it floats in open space.
LIDAR_CARPET_FLAG = 0x04
# Carpet: a light tint — LIGHTER than the floor, but toned down from pure white
# so it reads as a soft patch, not a highlight.
COLOR_LIDAR_CARPET = (235, 244, 252, 115)
# Lidar hits NOT adjacent to a saved-map wall are drawn a touch darker than the
# wall shade so they read as "observed but not a mapped wall"; hits touching a
# wall use the plain wall shade so they blend into it.
LIDAR_FLOAT_EXTRA = 40
# Inset each cell by this fraction of a grid cell per side (~0.5 px at scale 4),
# so cells read as separated marks rather than a solid block.
LIDAR_CELL_MARGIN = 0.125


def decode_obstacle_mask(compressed: bytes, width: int, height: int) -> set:
    """Set of (cx, cy) grid cells that are walls/obstacles in the SAVED map.

    A base-map pixel type (value & 0xFF) is a wall/border when bit 0x10 is set,
    or an obstacle when it equals 0x28. Used to decide whether a live lidar hit
    lines up with a real mapped wall.
    """
    decompressed = decompress_map(compressed)
    if not decompressed or width <= 0 or height <= 0:
        return set()
    pixels = _decode_packed_varints(decompressed)
    walls = set()
    limit = width * height
    for i, v in enumerate(pixels):
        if i >= limit:
            break
        pt = v & 0xFF
        if (pt & 0x10) or pt == 0x28:
            walls.add((i % width, i // width))
    return walls


def extend_lidar_layer(
    layer: Image.Image | None,
    mask: Image.Image | None,
    grid_width: int,
    grid_height: int,
    scale: int,
    base_img: Image.Image,
    new_cells: list[tuple[int, int, int]] | None,
    wall_cells_set: set | None = None,
    supersample: int = OVERLAY_SUPERSAMPLE,
) -> tuple[Image.Image, Image.Image]:
    """Draw only NEW lidar cells onto a persistent layer.

    Cells are ``(cx, cy, value)``. Each cell is drawn as a small inset square:
      - carpet (value & LIDAR_CARPET_FLAG) → a light tint (not a wall);
      - otherwise → the base colour darkened like a wall when the cell touches a
        saved-map wall (``wall_cells_set``, 3×3 neighbourhood), or darkened a bit
        MORE when it floats in open space (a lidar hit with no mapped wall).

    A binary ``mask`` ("L", 255 where a cell exists) is grown alongside the
    layer so render_overlay can paste-REPLACE without recomputing the mask
    from the whole layer every frame. Returns ``(layer, mask)``.
    """
    from PIL import Image, ImageDraw

    s = scale * max(1, supersample)
    if layer is None or mask is None:
        layer = Image.new("RGBA", (grid_width * s, grid_height * s), (0, 0, 0, 0))
        mask = Image.new("L", (grid_width * s, grid_height * s), 0)
    if new_cells:
        draw = ImageDraw.Draw(layer)
        mdraw = ImageDraw.Draw(mask)
        base_px = base_img.load()
        bw, bh = base_img.size
        walls = wall_cells_set or set()
        m = LIDAR_CELL_MARGIN
        for cx, cy, val in new_cells:
            if val & LIDAR_CARPET_FLAG:
                color = COLOR_LIDAR_CARPET
            else:
                bx = min(int((cx + 0.5) * scale), bw - 1)
                by = min(int((grid_height - 0.5 - cy) * scale), bh - 1)
                under = base_px[bx, by]
                touches = any(
                    (cx + dx, cy + dy) in walls
                    for dx in (-1, 0, 1)
                    for dy in (-1, 0, 1)
                )
                dk = LIDAR_DARKEN if touches else LIDAR_DARKEN + LIDAR_FLOAT_EXTRA
                color = (
                    max(0, under[0] - dk),
                    max(0, under[1] - dk),
                    max(0, under[2] - dk),
                    COLOR_LIDAR_WALL[3],
                )
            x0 = (cx + m) * s
            y0 = (grid_height - 1 - cy + m) * s
            x1 = (cx + 1 - m) * s - 1
            y1 = (grid_height - cy - m) * s - 1
            draw.rectangle([x0, y0, x1, y1], fill=color)
            mdraw.rectangle([x0, y0, x1, y1], fill=255)
    return layer, mask


def render_map_frame(
    base_img: Image.Image,
    grid_width: int,
    grid_height: int,
    *,
    scale: int,
    supersample: int = OVERLAY_SUPERSAMPLE,
    swath_layer: Image.Image | None,
    new_swath_quads: list[tuple] | None,
    show_swath: bool,
    lidar_layer: Image.Image | None,
    lidar_mask: Image.Image | None,
    new_wall_cells: list[tuple[int, int]] | None,
    show_lidar: bool,
    overlay_kwargs: dict,
    base_rgba: Image.Image | None = None,
    rail_trail: list | None = None,
    coord_trail: list | None = None,
    rail_trail_split: int = 0,
    wall_cells_set: set | None = None,
) -> tuple[bytes, Image.Image, Image.Image, Image.Image]:
    """Extend the persistent swath/lidar layers with new items, then render.

    Runs entirely in the render executor thread. Returns the PNG plus the
    (possibly newly created) persistent layers/mask so the camera can cache
    them. The layers are always extended — even when their switch is off — so
    toggling a layer back on instantly shows the full accumulated history
    without a re-scan; only compositing is gated by ``show_swath``/``show_lidar``.

    When ``rail_trail``/``coord_trail`` are given, the trail is composed HERE
    (in the executor) rather than on the event loop, so copying up to tens of
    thousands of points each frame doesn't block it. The lists are only read
    (C-atomic copies); a concurrent append can at worst shift the split by one
    point, which is cosmetic.
    """
    if rail_trail is not None and coord_trail is not None:
        import math

        rob_x = overlay_kwargs.get("robot_x")
        rob_y = overlay_kwargs.get("robot_y")
        if len(rail_trail) >= 2:
            trail = list(rail_trail)
            trail += list(coord_trail[rail_trail_split:])
            if rob_x is not None and rob_y is not None and trail:
                lx, ly = trail[-1]
                if math.hypot(rob_x - lx, rob_y - ly) > 0.3:
                    trail.append((rob_x, rob_y))
        else:
            trail = list(coord_trail) or None
        overlay_kwargs = {**overlay_kwargs, "trail": trail}

    swath_layer = extend_swath_layer(
        swath_layer, grid_width, grid_height, scale, new_swath_quads, supersample,
    )
    lidar_layer, lidar_mask = extend_lidar_layer(
        lidar_layer, lidar_mask, grid_width, grid_height, scale, base_img,
        new_wall_cells, wall_cells_set, supersample,
    )
    png = render_overlay(
        base_img,
        grid_width,
        grid_height,
        scale=scale,
        supersample=supersample,
        swath_layer=swath_layer if show_swath else None,
        lidar_layer=lidar_layer if show_lidar else None,
        lidar_mask=lidar_mask if show_lidar else None,
        base_rgba=base_rgba,
        **overlay_kwargs,
    )
    return png, swath_layer, lidar_layer, lidar_mask


def render_overlay(
    base_img: Image.Image,
    grid_width: int,
    grid_height: int,
    scale: int = 1,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    trail: list[tuple[float, float]] | None = None,
    zones: list[tuple[int, int, int, int]] | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    planned_path: list[tuple[float, float]] | None = None,
    cleaned_mask: Image.Image | None = None,
    swath_strips: list[tuple] | None = None,
    wall_cells: list[tuple[int, int]] | None = None,
    swath_layer: Image.Image | None = None,
    lidar_layer: Image.Image | None = None,
    lidar_mask: Image.Image | None = None,
    base_rgba: Image.Image | None = None,
    wall_cells_set: set | None = None,
    show_trail_line: bool = True,
    supersample: int = OVERLAY_SUPERSAMPLE,
) -> bytes:
    """Draw dynamic elements on a copy of the base map and return PNG bytes.

    All dynamic/vector elements (zones, cleaned area, planned path, trail,
    dock, robot) are drawn with float coordinates on a transparent RGBA
    layer supersampled ``supersample``× above the base image, then
    downsampled with BOX (area average = anti-aliasing) and composited.

    Coordinate convention: grid point (gx, gy) maps to the CENTER of its
    cell in image space — ((gx + 0.5) * s, (grid_height - 0.5 - gy) * s)
    with the Y axis flipped (grid Y grows up, image Y grows down).

    Args:
        base_img: Cached PIL Image from render_base_map (not modified);
            its size must be (grid_width * scale, grid_height * scale).
        grid_width: Map width in grid cells.
        grid_height: Map height in grid cells.
        scale: The upscale factor the base image was rendered with.
        robot_x: Robot X in grid coordinates.
        robot_y: Robot Y in grid coordinates.
        robot_heading: Heading in degrees.
        trail: (grid_x, grid_y) positions to draw as the cleaning path.
        zones: Rectangles (x_min, y_min, x_max, y_max) in grid coords to
            highlight as the area the robot will clean.
        dock_x: Dock X in grid coordinates.
        dock_y: Dock Y in grid coordinates.
        planned_path: Robot's planned trajectory polyline in grid coords,
            drawn as a thin translucent line under the trail.
        cleaned_mask: Optional PIL "L" image at native grid resolution
            (grid Y-up orientation, non-zero = cleaned) tinted translucent
            white under all other overlays.
        supersample: Extra supersample factor for the AA vector layer.

    Returns:
        PNG bytes of the composited image.
    """
    from PIL import Image, ImageDraw

    s = scale * max(1, supersample)
    layer = Image.new("RGBA", (grid_width * s, grid_height * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def to_img(gx: float, gy: float) -> tuple[float, float]:
        return ((gx + 0.5) * s, (grid_height - 0.5 - gy) * s)

    # Cleaned-area tint (bottom-most overlay)
    if cleaned_mask is not None:
        mask = cleaned_mask.transpose(Image.FLIP_TOP_BOTTOM).resize(
            layer.size, Image.NEAREST,
        )
        layer.paste(COLOR_CLEANED_TINT, mask=mask)

    # Target zones (semi-transparent amber fill + solid outline) — drawn UNDER
    # the vacuumed strip and lidar so cleaning progress and walls show ON TOP of
    # the highlighted zone instead of being hidden by the amber fill.
    if zones:
        for x_min, y_min, x_max, y_max in zones:
            gx0, gx1 = min(x_min, x_max), max(x_min, x_max)
            gy0, gy1 = min(y_min, y_max), max(y_min, y_max)
            px0 = gx0 * s
            px1 = (gx1 + 1) * s - 1
            py0 = (grid_height - 1 - gy1) * s
            py1 = (grid_height - gy0) * s - 1
            draw.rectangle(
                [px0, py0, px1, py1],
                fill=COLOR_ZONE_FILL,
                outline=COLOR_ZONE_OUTLINE,
                width=2 * s,
            )

    # Vacuumed strip: quads between the field-12 rail pair — the robot's
    # own record of the freshly vacuumed 11.4 cm track. ImageDraw on an
    # RGBA layer REPLACES pixels, so overlapping quads don't stack.
    # A pre-accumulated ``swath_layer`` (built incrementally by the camera so
    # per-frame cost stays O(new quads) instead of O(total)) is composited
    # directly; ``swath_strips`` is the stateless fallback for callers/tests.
    if swath_layer is not None:
        layer.alpha_composite(swath_layer)
    elif swath_strips:
        for quad in swath_strips:
            draw.polygon(
                [to_img(px, py) for px, py in quad], fill=COLOR_TRAIL_STRIP,
            )

    # Lidar wall/obstacle observations (field 7) — cell marks refining the
    # rasterized walls with what the robot actually measured. Each mark
    # takes the underlying base color darkened slightly MORE than the
    # rasterized walls (LIDAR_DARKEN vs the walls' 80), so on any floor it
    # reads as a wall-like, minimally darker translucent shade.
    # As with the swath, a pre-accumulated ``lidar_layer`` is composited when
    # provided (O(new cells)/frame); ``wall_cells`` is the stateless fallback.
    # Paste with a BINARY alpha mask so cell pixels REPLACE what's under them
    # (matching the fallback's draw.rectangle, which overwrites the swath) —
    # a plain alpha_composite would instead blend lidar over the swath. The
    # camera passes a ``lidar_mask`` grown alongside the layer; only recompute
    # it from the layer alpha when a caller supplies a layer without a mask.
    if lidar_layer is not None:
        if lidar_mask is None:
            lidar_mask = lidar_layer.getchannel("A").point(lambda a: 255 if a else 0)
        layer.paste(lidar_layer, (0, 0), lidar_mask)
    elif wall_cells:
        # Stateless fallback: same carpet/wall-touch logic as extend_lidar_layer.
        base_px = base_img.load()
        bw, bh = base_img.size
        walls = wall_cells_set or set()
        m = LIDAR_CELL_MARGIN
        for cx, cy, val in wall_cells:
            if val & LIDAR_CARPET_FLAG:
                color = COLOR_LIDAR_CARPET
            else:
                bx = min(int((cx + 0.5) * scale), bw - 1)
                by = min(int((grid_height - 0.5 - cy) * scale), bh - 1)
                under = base_px[bx, by]
                touches = any(
                    (cx + dx, cy + dy) in walls
                    for dx in (-1, 0, 1)
                    for dy in (-1, 0, 1)
                )
                dk = LIDAR_DARKEN if touches else LIDAR_DARKEN + LIDAR_FLOAT_EXTRA
                color = (
                    max(0, under[0] - dk),
                    max(0, under[1] - dk),
                    max(0, under[2] - dk),
                    COLOR_LIDAR_WALL[3],
                )
            x0 = (cx + m) * s
            y0 = (grid_height - 1 - cy + m) * s
            x1 = (cx + 1 - m) * s - 1
            y1 = (grid_height - cy - m) * s - 1
            draw.rectangle([x0, y0, x1, y1], fill=color)

    # Planned trajectory (thin translucent line under the trail)
    if planned_path and len(planned_path) >= 2:
        pts = [to_img(gx, gy) for gx, gy in planned_path]
        draw.line(pts, fill=COLOR_PLANNED_PATH, width=max(1, s // 3), joint="curve")

    # Trail (blue path showing where the robot has cleaned). Drawn as two
    # polylines — the older bulk and the bright recent tail — each in one
    # draw.line() with curved joints, instead of a per-segment Python loop
    # with manually drawn round joints (up to ~2200 iterations per frame).
    if show_trail_line and trail and len(trail) >= 2:
        render_trail = _decimate_trail(trail)
        trail_w = max(2, int(TRAIL_WIDTH * s))
        joint = "curve" if trail_w > 4 else None
        recent_start = max(len(render_trail) - TRAIL_RECENT_SEGMENTS, 0)
        pts = [to_img(gx, gy) for gx, gy in render_trail]
        old_pts = pts[: recent_start + 1]
        recent_pts = pts[recent_start:]
        if len(old_pts) >= 2:
            draw.line(old_pts, fill=COLOR_TRAIL_OLD, width=trail_w, joint=joint)
        if len(recent_pts) >= 2:
            draw.line(
                recent_pts, fill=COLOR_TRAIL_RECENT, width=trail_w, joint=joint,
            )

    # Dock (white circle, drawn before robot so robot renders on top)
    if dock_x is not None and dock_y is not None:
        dx, dy = to_img(dock_x, dock_y)
        radius = max(4, min(grid_width, grid_height) // 60) * s / 2
        draw.ellipse(
            [dx - radius, dy - radius, dx + radius, dy + radius],
            fill=(255, 255, 255, 255),
            outline=(180, 180, 180, 255),
            width=max(1, s // 2),
        )

    # Robot (blue circle + heading line)
    if (
        robot_x is not None
        and robot_y is not None
        and 0 <= robot_x < grid_width
        and 0 <= robot_y < grid_height
    ):
        import math

        rx, ry = to_img(robot_x, robot_y)
        radius = max(3, min(grid_width, grid_height) // 80) * s
        draw.ellipse(
            [rx - radius, ry - radius, rx + radius, ry + radius],
            fill=(0, 120, 255, 255),
            outline=(255, 255, 255, 255),
            width=max(1, s // 2),
        )
        if robot_heading is not None:
            rad = math.radians(robot_heading)
            arrow_len = radius * 2.5
            hx = rx + math.cos(rad) * arrow_len
            hy = ry - math.sin(rad) * arrow_len  # image Y-down
            draw.line(
                [(rx, ry), (hx, hy)],
                fill=(255, 255, 255, 255),
                width=max(2, s // 2),
            )

    # Downsample the supersampled layer (BOX = anti-aliasing) and composite.
    # base_rgba (cached by the camera) avoids a per-frame RGB→RGBA convert.
    if layer.size != base_img.size:
        layer = layer.resize(base_img.size, Image.BOX)
    base = base_rgba if base_rgba is not None else base_img.convert("RGBA")
    img = Image.alpha_composite(base, layer).convert("RGB")

    buf = io.BytesIO()
    # compress_level=1: PNG is lossless regardless, and level 1 encodes several
    # times faster than the default 6 — a fixed per-frame cost worth cutting.
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def compute_calibration_points(
    width: int,
    height: int,
    origin_x: int,
    origin_y: int,
    scale: int = 1,
) -> list[dict]:
    """Compute the 3-point camera calibration for xiaomi-vacuum-map-card.

    Maps image pixels of the rendered (scaled) map to robot world
    coordinates using the verified affine:
      world_x = grid_x + origin_x
      world_y = (height - 1 + origin_y) - grid_y_image   (Y-flip)

    Returns exactly three point pairs (origin, +x, +y) in the format the
    card expects for ``calibration_source: {camera: true}``.
    """
    return [
        {
            "vacuum": {"x": origin_x, "y": height - 1 + origin_y},
            "map": {"x": 0, "y": 0},
        },
        {
            "vacuum": {"x": width - 1 + origin_x, "y": height - 1 + origin_y},
            "map": {"x": (width - 1) * scale, "y": 0},
        },
        {
            "vacuum": {"x": origin_x, "y": origin_y},
            "map": {"x": 0, "y": (height - 1) * scale},
        },
    ]


def render_map_from_compressed(
    compressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
) -> bytes:
    """Decompress and render map data in one step (legacy interface).

    Args:
        compressed: Compressed map bytes from the robot.
        width: Map width in pixels.
        height: Map height in pixels.
        robot_x: Robot X position (optional).
        robot_y: Robot Y position (optional).
        robot_heading: Robot heading in degrees (optional).
        dock_x: Dock X position (optional).
        dock_y: Dock Y position (optional).
        room_names: Mapping of room_id to display name (optional).

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    decompressed = decompress_map(compressed)
    return render_map_png(
        decompressed, width, height, robot_x, robot_y, robot_heading,
        dock_x, dock_y, room_names,
    )
