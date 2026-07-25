"""Tests for compute_room_outlines — the camera `rooms` attribute source.

The output feeds xiaomi-vacuum-map-card's "Generate rooms config" editor
button: {room_id: {outline: [[wx, wy], ...], x, y}} in WORLD coordinates
(outline vertices on cell corners, collinear runs collapsed).
"""

from __future__ import annotations

import zlib

from narwal_client.map_renderer import _trace_cell_outline, compute_room_outlines


def _compress_grid(width: int, height: int, cell_fn) -> bytes:
    raw = bytearray()
    for y in range(height):
        for x in range(width):
            v = cell_fn(x, y)
            while v > 0x7F:
                raw.append((v & 0x7F) | 0x80)
                v >>= 7
            raw.append(v & 0x7F)
    ln = len(raw)
    lv = bytearray()
    while ln > 0x7F:
        lv.append((ln & 0x7F) | 0x80)
        ln >>= 7
    lv.append(ln & 0x7F)
    return zlib.compress(bytes([0x0A]) + bytes(lv) + bytes(raw))


class TestTraceCellOutline:
    def test_rectangle(self) -> None:
        cells = {(x, y) for x in range(2, 6) for y in range(3, 7)}
        outline = _trace_cell_outline(cells)
        assert sorted(map(tuple, outline)) == [(2, 3), (2, 7), (6, 3), (6, 7)]

    def test_l_shape_has_six_corners(self) -> None:
        cells = {(x, y) for x in range(0, 4) for y in range(0, 2)}
        cells |= {(x, y) for x in range(0, 2) for y in range(2, 4)}
        outline = _trace_cell_outline(cells)
        assert len(outline) == 6
        assert [0, 0] in outline and [4, 0] in outline and [0, 4] in outline

    def test_single_cell(self) -> None:
        outline = _trace_cell_outline({(5, 5)})
        assert sorted(map(tuple, outline)) == [(5, 5), (5, 6), (6, 5), (6, 6)]

    def test_empty(self) -> None:
        assert _trace_cell_outline(set()) == []


class TestComputeRoomOutlines:
    def test_two_rooms_world_coords(self) -> None:
        # Room 1: x 0..9, room 2: x 10..19 (full height 8)
        compressed = _compress_grid(
            20, 8, lambda x, y: ((1 if x < 10 else 2) << 8),
        )
        rooms = compute_room_outlines(compressed, 20, 8, origin_x=-47, origin_y=-217)
        assert set(rooms.keys()) == {"1", "2"}
        r1 = rooms["1"]
        # Rectangle corners shifted by origin
        assert sorted(map(tuple, r1["outline"])) == [
            (-47, -217), (-47, -209), (-37, -217), (-37, -209),
        ]
        # Centroid anchor in world coords
        assert r1["x"] == -42 and r1["y"] == -213

    def test_walls_count_into_their_room(self) -> None:
        # Border cells carry the wall flag (0x10) but belong to the room
        compressed = _compress_grid(
            6, 6,
            lambda x, y: (1 << 8) | (0x10 if x in (0, 5) or y in (0, 5) else 0),
        )
        rooms = compute_room_outlines(compressed, 6, 6)
        assert sorted(map(tuple, rooms["1"]["outline"])) == [
            (0, 0), (0, 6), (6, 0), (6, 6),
        ]

    def test_unassigned_and_empty_skipped(self) -> None:
        compressed = _compress_grid(4, 4, lambda x, y: 0x20 if x < 2 else 0)
        assert compute_room_outlines(compressed, 4, 4) == {}

    def test_junk_segment_ids_filtered(self) -> None:
        """Huge segment ids (special markers, seen live: 1048580) and tiny
        fragments are dropped."""
        def cell(x, y):
            if y < 2:
                return 1048580 << 8 & 0xFFFFFFFF or 0  # placeholder, replaced below
            return 1 << 8
        # room 1 fills y>=2; junk id occupies y<2
        compressed = _compress_grid(
            10, 6,
            lambda x, y: ((1048580 if y < 2 else 1) << 8),
        )
        rooms = compute_room_outlines(compressed, 10, 6)
        assert set(rooms.keys()) == {"1"}

    def test_tiny_fragment_filtered(self) -> None:
        compressed = _compress_grid(
            10, 6,
            lambda x, y: ((2 if (x, y) == (0, 0) else 1) << 8),
        )
        rooms = compute_room_outlines(compressed, 10, 6)
        assert "2" not in rooms and "1" in rooms

    def test_bad_data(self) -> None:
        assert compute_room_outlines(b"", 10, 10) == {}
        assert compute_room_outlines(b"garbage", 0, 0) == {}
