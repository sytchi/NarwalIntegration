"""Tests for the coordinator-owned cleaning trail (shared by map cameras).

Covers:
  - minimum-distance filter (TRAIL_MIN_DIST) instead of a timer
  - the first point is always recorded
  - a new cleaning session clears the trail
  - transient UNKNOWN status does not fake a session transition
  - the TRAIL_MAX_POINTS hard cap
"""

from __future__ import annotations

from unittest.mock import MagicMock

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.coordinator import (  # noqa: E402
    TRAIL_MAX_POINTS,
    NarwalCoordinator,
)
from custom_components.narwal.narwal_client.const import WorkingStatus  # noqa: E402
from custom_components.narwal.narwal_client.models import (  # noqa: E402
    MapData,
    MapDisplayData,
    NarwalState,
)


def _make_coordinator() -> NarwalCoordinator:
    coordinator = NarwalCoordinator.__new__(NarwalCoordinator)
    coordinator.trail = []
    coordinator._trail_last = None
    coordinator._was_cleaning_session = False
    coordinator.swath_quads = []
    coordinator._swath_seen = set()
    coordinator.rail_trail = []
    coordinator.rail_trail_split = 0
    coordinator.lidar_cells = []
    coordinator._lidar_seen = set()
    coordinator._lidar_map_ts = 0
    coordinator.client = MagicMock()
    return coordinator


def _make_state(
    robot_x: float,
    robot_y: float,
    status: WorkingStatus = WorkingStatus.CLEANING,
) -> NarwalState:
    state = NarwalState()
    state.working_status = status
    state.map_data = MapData(width=200, height=271, resolution=60,
                             origin_x=-47, origin_y=-217)
    state.map_display_data = MapDisplayData(robot_x=robot_x, robot_y=robot_y)
    return state


class TestTrailRecording:
    def test_first_point_always_recorded(self) -> None:
        coordinator = _make_coordinator()
        coordinator._update_trail(_make_state(10.0, 10.0))
        assert len(coordinator.trail) == 1
        # Grid coords: raw - origin
        assert coordinator.trail[0] == (10.0 - (-47), 10.0 - (-217))

    def test_min_distance_filter(self) -> None:
        coordinator = _make_coordinator()
        coordinator._update_trail(_make_state(10.0, 10.0))
        # Closer than 0.5 grid cells — not appended
        coordinator._update_trail(_make_state(10.2, 10.2))
        assert len(coordinator.trail) == 1
        # Farther than 0.5 — appended with full float precision
        coordinator._update_trail(_make_state(10.6, 10.3))
        assert len(coordinator.trail) == 2
        assert coordinator.trail[1] == (10.6 + 47, 10.3 + 217)

    def test_no_display_or_map_is_noop(self) -> None:
        coordinator = _make_coordinator()
        state = NarwalState()
        state.working_status = WorkingStatus.CLEANING
        coordinator._update_trail(state)
        assert coordinator.trail == []

    def test_zero_position_not_recorded(self) -> None:
        """to_grid_coords returns None for the (0, 0) no-fix position."""
        coordinator = _make_coordinator()
        coordinator._update_trail(_make_state(0.0, 0.0))
        assert coordinator.trail == []

    def test_max_points_cap(self) -> None:
        coordinator = _make_coordinator()
        coordinator.trail = [(0.0, 0.0)] * TRAIL_MAX_POINTS
        coordinator._trail_last = (0.0, 0.0)
        coordinator._was_cleaning_session = True
        coordinator._update_trail(_make_state(50.0, 50.0))
        assert len(coordinator.trail) == TRAIL_MAX_POINTS


class TestTrailSessionReset:
    def test_new_session_clears_trail(self) -> None:
        coordinator = _make_coordinator()
        coordinator.trail = [(1.0, 1.0), (2.0, 2.0)]
        coordinator._trail_last = (2.0, 2.0)
        coordinator._was_cleaning_session = False  # was idle
        coordinator._update_trail(_make_state(10.0, 10.0))
        # Old points wiped, new position recorded
        assert coordinator.trail == [(10.0 + 47, 10.0 + 217)]

    def test_ongoing_session_keeps_trail(self) -> None:
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = True
        coordinator._update_trail(_make_state(10.0, 10.0))
        coordinator._update_trail(_make_state(20.0, 20.0))
        assert len(coordinator.trail) == 2

    def test_unknown_status_does_not_reset(self) -> None:
        """A broadcast dropout (UNKNOWN) must not fake a session transition."""
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = True
        coordinator._update_trail(_make_state(10.0, 10.0))
        # Transient UNKNOWN — the was-cleaning flag must survive
        coordinator._update_trail(
            _make_state(20.0, 20.0, status=WorkingStatus.UNKNOWN),
        )
        assert coordinator._was_cleaning_session is True
        # Back to cleaning — same session, trail NOT cleared
        coordinator._update_trail(_make_state(30.0, 30.0))
        assert len(coordinator.trail) == 3


class TestMapLayers:
    def test_quads_accumulate_and_dedupe(self) -> None:
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = True
        state = _make_state(10.0, 10.0)
        state.map_display_data.rail_paths = [
            [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)],
            [(0.0, 2.28), (1.0, 2.28), (2.0, 2.28)],
        ]
        coordinator._update_trail(state)
        assert len(coordinator.swath_quads) == 2
        # Same window again (sliding overlap) — deduped, nothing added
        coordinator._update_trail(state)
        assert len(coordinator.swath_quads) == 2
        # Quads are stored in grid coords (world - origin)
        assert coordinator.swath_quads[0][0] == (0.0 + 47, 0.0 + 217)

    def test_lidar_accumulates_and_resets_on_map_change(self) -> None:
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = True
        state = _make_state(10.0, 10.0)
        state.map_data.created_at = 111
        # wall_cells are (index, value) pairs
        state.map_display_data.wall_cells = [(0, 257), (201, 259), (402, 257)]
        coordinator._update_trail(state)
        # stride = map width (200): idx 201 -> (1, 1); cells are (cx, cy, value)
        assert any(c[:2] == (1, 1) for c in coordinator.lidar_cells)
        assert len(coordinator.lidar_cells) == 3
        # New saved map -> lidar accumulation resets
        state.map_data.created_at = 222
        state.map_display_data.wall_cells = [(5, 513)]
        coordinator._update_trail(state)
        assert coordinator.lidar_cells == [(5, 0, 513)]

    def test_new_session_clears_quads_and_noncarpet_lidar_keeps_carpet(self) -> None:
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = False
        coordinator.swath_quads = [((0, 0), (1, 0), (1, 1), (0, 1))]
        coordinator._swath_seen = {(0, 0)}
        # (3,3) carpet (bit2 set, 0x105); (4,4) plain wall (0x101, no bit2)
        coordinator.lidar_cells = [(3, 3, 0x105), (4, 4, 0x101)]
        coordinator._lidar_seen = {(3, 3), (4, 4)}
        coordinator._lidar_map_ts = 0
        coordinator._update_trail(_make_state(10.0, 10.0))
        assert coordinator.swath_quads == []
        # New session drops non-carpet lidar (4,4) but keeps carpet (3,3)
        assert any(c[:2] == (3, 3) for c in coordinator.lidar_cells)
        assert not any(c[:2] == (4, 4) for c in coordinator.lidar_cells)
        assert (4, 4) not in coordinator._lidar_seen


class TestRailTrail:
    def test_midline_accumulates_with_quads(self) -> None:
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = True
        state = _make_state(10.0, 10.0)
        state.map_display_data.rail_paths = [
            [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)],
            [(0.0, 2.28), (1.0, 2.28), (2.0, 2.28)],
        ]
        coordinator._update_trail(state)
        # 2 segments -> seed point + 2 midpoints, at rail midline (y=1.14)
        assert len(coordinator.rail_trail) == 3
        assert coordinator.rail_trail[0] == (0.0 + 47, 1.14 + 217)
        assert coordinator.rail_trail[-1] == (2.0 + 47, 1.14 + 217)
        # Overlapping window again -> deduped, nothing appended
        coordinator._update_trail(state)
        assert len(coordinator.rail_trail) == 3

    def test_cleared_on_new_session(self) -> None:
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = False
        coordinator.rail_trail = [(1.0, 1.0)]
        coordinator._update_trail(_make_state(10.0, 10.0))
        assert coordinator.rail_trail == []


class TestRailTrailSplit:
    def test_split_tracks_rail_updates(self) -> None:
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = True
        # Poses only — tail grows, no rails yet
        coordinator._update_trail(_make_state(10.0, 10.0))
        coordinator._update_trail(_make_state(12.0, 12.0))
        assert coordinator.rail_trail_split == 0
        # Rails arrive — everything recorded so far is superseded
        state = _make_state(14.0, 14.0)
        state.map_display_data.rail_paths = [
            [(10.0, 10.0), (12.0, 12.0)],
            [(10.0, 12.28), (12.0, 14.28)],
        ]
        coordinator._update_trail(state)
        # split = trail length BEFORE this frame's pose append (2)
        assert coordinator.rail_trail_split == 2
        assert len(coordinator.trail) == 3  # this frame's pose appended after
        # More poses extend the tail beyond the split
        coordinator._update_trail(_make_state(16.0, 16.0))
        assert coordinator.trail[coordinator.rail_trail_split:] == [
            (14.0 + 47, 14.0 + 217), (16.0 + 47, 16.0 + 217),
        ]

    def test_split_reset_on_new_session(self) -> None:
        coordinator = _make_coordinator()
        coordinator._was_cleaning_session = False
        coordinator.rail_trail_split = 7
        coordinator._update_trail(_make_state(10.0, 10.0))
        assert coordinator.rail_trail_split == 0
