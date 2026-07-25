"""Tests for the incremental swath/lidar accumulation layers.

The vacuumed strip (field 12) and lidar wall cells (field 7) only grow during
a cleaning session, so the camera draws only the NEW items onto persistent
layers each frame (``extend_swath_layer`` / ``extend_lidar_layer`` /
``render_map_frame``) instead of re-drawing the whole set with ``render_overlay``.

These tests pin the two guarantees that makes safe:
  1. Output is pixel-identical to the stateless full-redraw path.
  2. Per-frame cost stays flat as the accumulated set grows (no O(total) redraw).
"""

from __future__ import annotations

import io
import time

from PIL import Image

from narwal_client.map_renderer import (
    OVERLAY_SUPERSAMPLE,
    extend_lidar_layer,
    extend_swath_layer,
    render_map_frame,
    render_overlay,
)

GW, GH, SCALE = 40, 50, 4

# Overlay params shared by both the full-redraw and incremental paths so the
# only thing under test is how swath/lidar get onto the frame.
_OVERLAY = {
    "robot_x": 20.0,
    "robot_y": 25.0,
    "robot_heading": 30.0,
    "trail": [(10.0, 10.0), (22.0, 24.0), (28.0, 20.0)],
    "zones": None,
    "dock_x": 12.0,
    "dock_y": 12.0,
    "planned_path": None,
    "show_trail_line": True,
}


def _varied_base() -> Image.Image:
    """Base image with per-cell colour variation so lidar darkening is visible
    (a flat base would hide swath/lidar-order differences)."""
    img = Image.new("RGB", (GW * SCALE, GH * SCALE), (170, 180, 190))
    px = img.load()
    for gx in range(GW):
        for gy in range(GH):
            shade = 120 + (gx * 7 + gy * 3) % 90
            for dx in range(SCALE):
                for dy in range(SCALE):
                    px[gx * SCALE + dx, gy * SCALE + dy] = (shade, shade, shade)
    return img


def _cells(n: int) -> list[tuple[int, int, int]]:
    # (cx, cy, value) — value cycles a few classifications for colour variety.
    return [(i % GW, (i * 3) % GH, 257 + (i % 6) * 2) for i in range(n)]


def _quads(m: int) -> list[list[tuple[float, float]]]:
    out = []
    for i in range(m):
        cx = 3.0 + (i % (GW - 6))
        cy = 3.0 + (i % (GH - 6))
        out.append([(cx, cy), (cx + 2, cy), (cx + 2, cy + 1.2), (cx, cy + 1.2)])
    return out


def _pixels(png: bytes) -> bytes:
    return Image.open(io.BytesIO(png)).convert("RGB").tobytes()


def _render_incremental(cells, quads, *, chunks=5, show_swath=True, show_lidar=True):
    """Feed cells/quads in `chunks` frames (mimicking accumulation) and return
    the final PNG, exactly as the camera drives render_map_frame."""
    base = _varied_base()
    swath_layer = lidar_layer = lidar_mask = None
    drawn: set[tuple[int, int]] = set()
    png = b""
    for k in range(chunks):
        c0, c1 = len(cells) * k // chunks, len(cells) * (k + 1) // chunks
        q0, q1 = len(quads) * k // chunks, len(quads) * (k + 1) // chunks
        new_cells = [c for c in cells[c0:c1] if c not in drawn]
        png, swath_layer, lidar_layer, lidar_mask = render_map_frame(
            base, GW, GH, scale=SCALE,
            swath_layer=swath_layer, new_swath_quads=quads[q0:q1],
            show_swath=show_swath,
            lidar_layer=lidar_layer, lidar_mask=lidar_mask,
            new_wall_cells=new_cells,
            show_lidar=show_lidar,
            overlay_kwargs=_OVERLAY,
        )
        drawn.update(new_cells)
    return png


def _render_full(cells, quads, *, show_swath=True, show_lidar=True):
    base = _varied_base()
    return render_overlay(
        base, GW, GH, scale=SCALE,
        wall_cells=list(cells) if show_lidar else None,
        swath_strips=quads if show_swath else None,
        **_OVERLAY,
    )


class TestPixelIdentical:
    """Incremental accumulation must match the stateless full redraw exactly."""

    def test_lidar_only_identical(self) -> None:
        cells, quads = _cells(300), []
        assert _pixels(_render_incremental(cells, quads, show_swath=False)) == \
            _pixels(_render_full(cells, quads, show_swath=False))

    def test_swath_only_identical(self) -> None:
        cells, quads = [], _quads(150)
        assert _pixels(_render_incremental(cells, quads, show_lidar=False)) == \
            _pixels(_render_full(cells, quads, show_lidar=False))

    def test_both_layers_identical(self) -> None:
        # Lidar must REPLACE (not blend over) the swath where they overlap,
        # matching render_overlay's draw order — a plain alpha_composite would
        # differ here.
        cells, quads = _cells(300), _quads(150)
        assert _pixels(_render_incremental(cells, quads)) == \
            _pixels(_render_full(cells, quads))

    def test_switch_off_hides_layer(self) -> None:
        # Accumulating cells but rendering with show_lidar=False must match a
        # render that never had lidar at all.
        cells, quads = _cells(300), _quads(150)
        assert _pixels(_render_incremental(cells, quads, show_lidar=False)) == \
            _pixels(_render_full([], quads, show_lidar=False))


class TestExtendLayers:
    def test_layer_created_at_supersample_size(self) -> None:
        base = _varied_base()
        s = SCALE * OVERLAY_SUPERSAMPLE
        layer, mask = extend_lidar_layer(None, None, GW, GH, SCALE, base, _cells(10))
        assert layer.mode == "RGBA"
        assert layer.size == (GW * s, GH * s)
        assert mask.mode == "L"
        assert mask.size == (GW * s, GH * s)

    def test_swath_layer_created_and_reused(self) -> None:
        first = extend_swath_layer(None, GW, GH, SCALE, _quads(5))
        again = extend_swath_layer(first, GW, GH, SCALE, _quads(5))
        # Same object is extended in place, not reallocated.
        assert again is first

    def test_empty_new_items_returns_layer_unchanged(self) -> None:
        base = _varied_base()
        layer, mask = extend_lidar_layer(None, None, GW, GH, SCALE, base, [])
        before = layer.tobytes()
        layer2, mask2 = extend_lidar_layer(layer, mask, GW, GH, SCALE, base, [])
        assert layer2 is layer
        assert mask2 is mask
        assert layer2.tobytes() == before

    def test_grown_mask_equals_recomputed(self) -> None:
        # The incrementally-grown binary mask must equal recomputing it from
        # the layer's alpha channel — that equality is what lets render_overlay
        # skip the per-frame getchannel().point() recompute with no visual change.
        base = _varied_base()
        layer, mask = extend_lidar_layer(None, None, GW, GH, SCALE, base, _cells(300))
        recomputed = layer.getchannel("A").point(lambda a: 255 if a else 0)
        assert mask.tobytes() == recomputed.tobytes()


class TestPerFrameCostFlat:
    """The whole point: a frame that adds a few cells costs about the same
    whether the layer already holds 200 cells or 2000 — unlike the full redraw,
    whose cost grows with the total."""

    def _time(self, fn, reps=3) -> float:
        best = float("inf")
        for _ in range(reps):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        return best

    def test_incremental_frame_does_not_grow_with_accumulation(self) -> None:
        base = _varied_base()
        small_layer, small_mask = extend_lidar_layer(
            None, None, GW, GH, SCALE, base, _cells(200))
        big_layer, big_mask = extend_lidar_layer(
            None, None, GW, GH, SCALE, base, _cells(1900))

        def frame(layer, mask):
            return render_map_frame(
                base, GW, GH, scale=SCALE,
                swath_layer=None, new_swath_quads=None, show_swath=False,
                lidar_layer=layer, lidar_mask=mask,
                new_wall_cells=[(39, 49, 257)], show_lidar=True,
                overlay_kwargs=_OVERLAY,
            )

        t_small = self._time(lambda: frame(small_layer, small_mask))
        t_big = self._time(lambda: frame(big_layer, big_mask))
        # Adding one cell to a big layer must not be dramatically slower than
        # adding one to a small layer (allow generous headroom for noise).
        assert t_big < t_small * 2.5 + 0.05
