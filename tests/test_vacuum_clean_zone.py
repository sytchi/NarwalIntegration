"""Tests for narwal.clean_zone (async_clean_zone) — coordinate contracts.

The `coordinates` parameter selects the contract: "pixels" (DEFAULT —
the pre-1.6 map-image-pixel behavior, kept for backwards compatibility),
"world" (the new mode, what the card sends with camera calibration), or
"auto" (range-based detection). Negative values are impossible as pixels
and are treated as world even in pixels mode.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.vacuum import NarwalVacuum  # noqa: E402
from narwal_client.const import CleanMode  # noqa: E402
from narwal_client.models import NarwalState  # noqa: E402


def _make_vacuum(state: NarwalState | None = None) -> NarwalVacuum:
    """Create a NarwalVacuum with mocked coordinator."""
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_dev_001"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.clean_mode = "sweep_mop"
    coordinator.client = MagicMock()
    coordinator.client.robot_awake = True
    coordinator.client.start_zone = AsyncMock(
        return_value=MagicMock(result_code=0, success=True)
    )
    coordinator.last_update_success = True

    vac = NarwalVacuum.__new__(NarwalVacuum)
    vac.coordinator = coordinator
    vac._attr_unique_id = "test_dev_001"
    vac._attr_device_info = {}
    vac._last_fan_speed = None
    vac.async_write_ha_state = MagicMock()
    vac._ensure_awake = AsyncMock()

    return vac


class TestCleanZoneWorldPassthrough:
    async def test_zone_list_passed_through_unchanged(self) -> None:
        """World rects (incl. negatives) reach start_zone without transform."""
        vac = _make_vacuum()
        await vac.async_clean_zone(
            zone=[[-21, -23, 29, 29], [10, -120, 40, -90]], coordinates="world",
        )
        args, kwargs = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(-21, -23, 29, 29), (10, -120, 40, -90)]
        assert kwargs["clean_mode"] == CleanMode.SWEEP_MOP

    async def test_x1_y2_single_rect(self) -> None:
        vac = _make_vacuum()
        await vac.async_clean_zone(x1=-5, y1=-10, x2=15, y2=20, coordinates="world")
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(-5, -10, 15, 20)]

    async def test_float_coords_rounded(self) -> None:
        """Card may send floats (rounding off) — they are rounded to ints."""
        vac = _make_vacuum()
        await vac.async_clean_zone(
            zone=[[-20.6, -22.4, 28.5, 29.49]], coordinates="world",
        )
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(-21, -22, 28, 29)]

    async def test_no_rect_is_noop(self) -> None:
        vac = _make_vacuum()
        await vac.async_clean_zone()
        vac.coordinator.client.start_zone.assert_not_awaited()

    async def test_negative_world_needs_no_map(self) -> None:
        """Rects with negative values are world — no get_map for detection."""
        vac = _make_vacuum(state=None)
        await vac.async_clean_zone(zone=[[-5, 0, 10, 10]], coordinates="auto")
        vac.coordinator.client.start_zone.assert_awaited_once()
        vac.coordinator.client.get_map.assert_not_called()

    async def test_default_is_pixels(self) -> None:
        """No parameter = the pre-1.6 pixel contract (backwards compat)."""
        vac = _make_vacuum(state=MagicMock())
        map_data = MagicMock()
        map_data.width = 200
        map_data.height = 271
        map_data.origin_x = -47
        map_data.origin_y = -217
        vac.coordinator.data.map_data = map_data
        await vac.async_clean_zone(zone=[[120, 197, 161, 210]])
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(73, -157, 114, -144)]

    async def test_default_pixels_negative_safety(self) -> None:
        """Negative values are impossible as pixels — treated as world even
        without the parameter (protects world callers that forget it)."""
        vac = _make_vacuum(state=None)
        await vac.async_clean_zone(zone=[[-21, -23, 29, 29]])
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(-21, -23, 29, 29)]
        vac.coordinator.client.get_map.assert_not_called()


class TestLegacyPixelDetection:
    def _vac_with_map(self):
        vac = _make_vacuum(state=MagicMock())
        map_data = MagicMock()
        map_data.width = 200
        map_data.height = 271
        map_data.origin_x = -47
        map_data.origin_y = -217
        vac.coordinator.data.map_data = map_data
        return vac

    async def test_legacy_pixels_converted(self) -> None:
        """Old identity-px rect (values above the world range) is converted
        with the pre-1.6 transform. Real values: the Bar zone."""
        vac = self._vac_with_map()
        await vac.async_clean_zone(zone=[[120, 197, 161, 210]], coordinates="auto")
        args, _ = vac.coordinator.client.start_zone.await_args
        # to_world(120,197)=(73,-144), to_world(161,210)=(114,-157)
        assert args[0] == [(73, -157, 114, -144)]

    async def test_ambiguous_treated_as_world(self) -> None:
        """In auto mode a rect that fits both ranges stays world."""
        vac = self._vac_with_map()
        await vac.async_clean_zone(zone=[[0, 0, 10, 10]], coordinates="auto")
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(0, 0, 10, 10)]

    async def test_explicit_world_skips_detection(self) -> None:
        """coordinates=world passes big positive values through untouched
        (auto would have classified them as legacy pixels)."""
        vac = self._vac_with_map()
        await vac.async_clean_zone(zone=[[120, 197, 161, 210]], coordinates="world")
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(120, 197, 161, 210)]

    async def test_explicit_pixels_always_converts(self) -> None:
        """coordinates=pixels converts even ambiguous (in-range) rects,
        which auto would have treated as world."""
        vac = self._vac_with_map()
        await vac.async_clean_zone(zone=[[0, 0, 10, 10]], coordinates="pixels")
        args, _ = vac.coordinator.client.start_zone.await_args
        # to_world(0,0)=(-47,53), to_world(10,10)=(-37,43)
        assert args[0] == [(-47, 43, -37, 53)]

    async def test_explicit_pixels_without_map_aborts(self) -> None:
        """Pixels cannot be converted without a map — no start_zone call."""
        vac = _make_vacuum(state=None)
        vac.coordinator.client.get_map = AsyncMock(side_effect=Exception("asleep"))
        await vac.async_clean_zone(zone=[[10, 10, 20, 20]], coordinates="pixels")
        vac.coordinator.client.start_zone.assert_not_awaited()

    async def test_no_map_available_assumes_world(self) -> None:
        """Auto detection impossible without a map — pass through as world."""
        vac = _make_vacuum(state=None)
        vac.coordinator.client.get_map = AsyncMock(side_effect=Exception("asleep"))
        await vac.async_clean_zone(zone=[[120, 197, 161, 210]], coordinates="auto")
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(120, 197, 161, 210)]
