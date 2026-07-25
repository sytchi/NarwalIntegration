"""Tests for PlannedTrajectory (status/point_navi_plan_traj decoder).

Fixture values come from a LIVE capture (2026-07-22, kitchen clean on
fw v01.08.03.07): blackboxprotobuf-decoded broadcast payloads with
fixed32 floats represented as ints, exactly as the client hands them
to from_broadcast().
"""

from __future__ import annotations

import pytest

from narwal_client.models import PlannedTrajectory

# Real 5-point frame captured live (robot driving toward the kitchen).
REAL_FRAME = {
    "1": [
        {"1": 1096606764, "2": 3262898811},
        {"1": 1097380854, "2": 3263645578},
        {"1": 1098197610, "2": 3264256578},
        {"1": 1098962833, "2": 3264865982},
        {"1": 1099010782, "2": 3264937153},
    ]
}

REAL_POINTS = [
    (13.805706024169922, -62.97117233276367),
    (14.543935775756836, -67.63972473144531),
    (15.322854995727539, -72.30128479003906),
    (16.105257034301758, -76.95066833496094),
    (16.196712493896484, -77.49365997314453),
]


class TestPlannedTrajectoryDecode:
    def test_real_frame(self) -> None:
        traj = PlannedTrajectory.from_broadcast(REAL_FRAME)
        assert len(traj.points) == 5
        for got, expected in zip(traj.points, REAL_POINTS, strict=True):
            assert got[0] == pytest.approx(expected[0])
            assert got[1] == pytest.approx(expected[1])

    def test_single_dict_item(self) -> None:
        """bbpb returns a bare dict (not a list) for single-element repeats."""
        traj = PlannedTrajectory.from_broadcast({"1": {"1": 1096606764, "2": 3262898811}})
        assert len(traj.points) == 1
        assert traj.points[0][0] == pytest.approx(REAL_POINTS[0][0])

    def test_empty_frame(self) -> None:
        """A zero-point frame (seen live during segment transitions)."""
        assert PlannedTrajectory.from_broadcast({}).points == []
        assert PlannedTrajectory.from_broadcast({"1": []}).points == []

    def test_malformed_items_skipped(self) -> None:
        traj = PlannedTrajectory.from_broadcast(
            {"1": [{"1": 1096606764}, "garbage", {"2": 3262898811}]}
        )
        assert traj.points == []
