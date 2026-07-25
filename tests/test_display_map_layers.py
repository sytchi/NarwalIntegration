"""Tests for display_map field 12 (rail paths) and field 7 (lidar walls).

Fixtures are REAL captured payload fragments (2026-07-22 kitchen clean,
fw v01.08.03.07) in the exact shape blackboxprotobuf hands to
MapDisplayData.from_broadcast (packed float32 / zlib blobs as bytes).
"""

from __future__ import annotations

import base64

import pytest

from narwal_client.models import MapDisplayData, _decode_wall_cells

# Field 12 rails from a live frame (10 points each, first rail starts at
# the dock ≈ (-9.85, 0.46) world)
F12_X0 = base64.b64decode("Ma0dwa69bb83CUG/WWXQv3djYb+wYF4/4xceQA9ph0BMI7pA8iviQA==")
F12_Y0 = base64.b64decode("jWbtPvT7HD/cIza/jPj8vFbCKj/Uar8+LG4qv8WAK8BsAbnAB58twQ==")
F12_X1 = base64.b64decode("IdkdwWiL3b91zJw/++aQv/y9xb9Er+y+w4pCP1/cDUAlXmhAaH2ZQA==")
F12_Y1 = base64.b64decode("2Onov2Ilw79x2Oy/nZ4QwEA9wr+FoL2/SG0LwJc8cMDGGs7AsV0xwQ==")

# Field 7 blob from the same frame (zlib(protobuf{1: idx varints, 2: values}))
F7_BLOB = base64.b64decode(
    "eAFlkGtPE0EUhpPHfiD70X+8IRqp2nZZRNtFoqG2FhAjRkAQIa2wza5LuNQGA5ZIuSi0"
    "s7Q41GggzrBcjObJnMy857zvzK5RudEbsLOCrHBSoVilZ53TAxYbhE2SgpTAEvQKbEGf"
    "YDJkKuRtyJeQWojZ4k6LnhbxFpkW25K6ZEeyK9mT7Eu+Sb5LDiSHkoakKRGSUHIkaUna"
    "kmPJdIeZDrNlbnn4Ph99Ap9EQDIgFWAFdCr8rFCrkltHfGZig7NNjprcFyQEDwQPBRnB"
    "VsjXkO2QeptBjz2fsYCTJd4vc3uVpTX6P3F3jntzVOZ5XGS3xFmJ0QV+LJJwSbpYLr0u"
    "1TIDyxQbxAUrIUOTHE7xeprfM5Rm+VBkochOCcelXmbYw/N5s8HpJnmP5x4FjxEPp0B9"
    "mJFRjl+QGKP6ksFX7I3TM4ft8q5MKUsux1oekWeiwNkwi6OszbNZJuvxzCPnYTtsDLD6"
    "lMwQtSxbWfI57BQ1i3YfM/10P8JPc5Bm3OHXAMVB4k/YH+fmFKZhGt0K07DU+hfHcAxb"
    "oaue0lWf9f5/dCdC+6Lk60RLOUylX6Lv1d20ke5SWpfTZV31rmciv37blTvmxHR6dP9f"
    "742ZMdswVVWcd6N9tzpFXKbahnWu6ZRLTU3E7AuvUlGoXlr9Ff21F36tRhh/AFvKUSk="
)


class TestRailPaths:
    def test_two_rails_parsed(self) -> None:
        decoded = {
            "12": [
                {"1": {"1": F12_X0, "2": F12_Y0}, "3": 1},
                {"1": {"1": F12_X1, "2": F12_Y1}, "3": 2},
            ]
        }
        d = MapDisplayData.from_broadcast(decoded)
        assert len(d.rail_paths) == 2
        assert len(d.rail_paths[0]) == 10
        # First rail starts at the dock (validated live)
        assert d.rail_paths[0][0][0] == pytest.approx(-9.8548, abs=1e-3)
        assert d.rail_paths[0][0][1] == pytest.approx(0.4637, abs=1e-3)

    def test_latin1_str_blobs(self) -> None:
        """bbpb sometimes returns bytes fields as latin1 strings."""
        decoded = {
            "12": {"1": {"1": F12_X0.decode("latin1"), "2": F12_Y0.decode("latin1")}}
        }
        d = MapDisplayData.from_broadcast(decoded)
        assert len(d.rail_paths) == 1
        assert len(d.rail_paths[0]) == 10

    def test_no_field12(self) -> None:
        assert MapDisplayData.from_broadcast({}).rail_paths == []


class TestWallCells:
    def test_decode_real_blob(self) -> None:
        pairs = _decode_wall_cells(F7_BLOB)
        assert len(pairs) == 156
        # (index, value) pairs — indexes unchanged, values now preserved
        idxs = [idx for idx, _val in pairs]
        assert idxs[:5] == [42642, 43236, 43637, 43638, 43842]
        # All indexes fit the 200x271 map; every cell carries a value
        assert all(0 <= i < 200 * 271 for i in idxs)
        assert all(isinstance(v, int) for _i, v in pairs)

    def test_field7_in_broadcast(self) -> None:
        decoded = {"7": {"1": 44, "2": 50, "3": F7_BLOB}}
        d = MapDisplayData.from_broadcast(decoded)
        assert len(d.wall_cells) == 156

    def test_garbage_blob(self) -> None:
        assert _decode_wall_cells(b"notzlib") == []
        assert _decode_wall_cells(None) == []
