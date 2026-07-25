"""Tests for NarwalState.best_robot_position() — freshest-source robot pose.

display_map poses drop out for 30s+ during cleaning while
point_navi_plan_traj keeps flowing; whichever broadcast arrived most
recently provides the rendered pose. Trajectory heads outside the map
bbox (docking anomaly frames) are rejected.
"""

from __future__ import annotations

import pytest

from narwal_client.models import (
    MapData,
    MapDisplayData,
    NarwalState,
    PlannedTrajectory,
)


def _state(
    display: MapDisplayData | None = None,
    traj: PlannedTrajectory | None = None,
    with_map: bool = True,
) -> NarwalState:
    state = NarwalState()
    state.map_display_data = display
    state.planned_trajectory = traj
    if with_map:
        state.map_data = MapData(width=200, height=271, origin_x=-47, origin_y=-217)
    return state


class TestBestRobotPosition:
    def test_none_when_no_sources(self) -> None:
        assert _state().best_robot_position() is None

    def test_display_only(self) -> None:
        display = MapDisplayData(
            robot_x=10.0, robot_y=-20.0, robot_heading=90.0, received_at=100.0,
        )
        pose = _state(display=display).best_robot_position()
        assert pose == (10.0, -20.0, 90.0)

    def test_zero_display_pose_ignored(self) -> None:
        display = MapDisplayData(robot_x=0.0, robot_y=0.0, received_at=100.0)
        assert _state(display=display).best_robot_position() is None

    def test_traj_only(self) -> None:
        traj = PlannedTrajectory(
            points=[(48.05, -109.16), (47.49, -109.21)], received_at=100.0,
        )
        pose = _state(traj=traj).best_robot_position()
        assert pose is not None
        assert pose[0] == pytest.approx(48.05)
        assert pose[1] == pytest.approx(-109.16)
        # Heading from the first segment (atan2 in world coords)
        assert pose[2] == pytest.approx(-174.9, abs=0.5)

    def test_fresher_display_wins(self) -> None:
        display = MapDisplayData(
            robot_x=10.0, robot_y=-20.0, robot_heading=0.0, received_at=200.0,
        )
        traj = PlannedTrajectory(points=[(11.0, -21.0), (12.0, -22.0)], received_at=100.0)
        pose = _state(display=display, traj=traj).best_robot_position()
        assert pose[:2] == (10.0, -20.0)

    def test_fresher_traj_wins(self) -> None:
        """The dropout scenario: display stale, trajectory keeps flowing."""
        display = MapDisplayData(
            robot_x=17.02, robot_y=-80.58, robot_heading=0.0, received_at=100.0,
        )
        traj = PlannedTrajectory(
            points=[(41.90, -109.82), (42.88, -109.87)], received_at=130.0,
        )
        pose = _state(display=display, traj=traj).best_robot_position()
        assert pose[0] == pytest.approx(41.90)
        assert pose[1] == pytest.approx(-109.82)

    def test_out_of_bbox_traj_head_rejected(self) -> None:
        """Docking anomaly frames (out-of-bbox points) must not win."""
        display = MapDisplayData(
            robot_x=43.09, robot_y=-176.36, robot_heading=0.0, received_at=100.0,
        )
        # Real anomaly values captured live: positive Y on a Y-negative map
        traj = PlannedTrajectory(
            points=[(22.97, 175.20), (22.97, 175.20)], received_at=200.0,
        )
        pose = _state(display=display, traj=traj).best_robot_position()
        assert pose[:2] == (43.09, -176.36)

    def test_identical_points_heading_falls_back_to_display(self) -> None:
        display = MapDisplayData(
            robot_x=10.0, robot_y=-20.0, robot_heading=45.0, received_at=100.0,
        )
        traj = PlannedTrajectory(
            points=[(11.0, -21.0), (11.0, -21.0)], received_at=200.0,
        )
        pose = _state(display=display, traj=traj).best_robot_position()
        assert pose == (11.0, -21.0, 45.0)

    def test_no_map_data_accepts_traj(self) -> None:
        """Without MapData there is no bbox to validate against — accept."""
        traj = PlannedTrajectory(points=[(5.0, -5.0), (6.0, -6.0)], received_at=1.0)
        pose = _state(traj=traj, with_map=False).best_robot_position()
        assert pose is not None
        assert pose[:2] == (5.0, -5.0)
