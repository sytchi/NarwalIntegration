"""Tests for narwal.clean_zone (async_clean_zone).

Since 2.0.0 zones are ALWAYS robot world coordinates. The legacy map-image
pixel contract (and its "auto"/"pixels" detection/conversion) was removed;
the `coordinates` parameter is still accepted so pre-2.0 calls don't error,
but its value is ignored — nothing is ever converted and the map is never
fetched for coordinate conversion.
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
    coordinator.client.get_map = AsyncMock()
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
            zone=[[-21, -23, 29, 29], [10, -120, 40, -90]],
        )
        args, kwargs = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(-21, -23, 29, 29), (10, -120, 40, -90)]
        assert kwargs["clean_mode"] == CleanMode.SWEEP_MOP

    async def test_x1_y2_single_rect(self) -> None:
        vac = _make_vacuum()
        await vac.async_clean_zone(x1=-5, y1=-10, x2=15, y2=20)
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(-5, -10, 15, 20)]

    async def test_float_coords_rounded(self) -> None:
        """Card may send floats (rounding off) — they are rounded to ints."""
        vac = _make_vacuum()
        await vac.async_clean_zone(zone=[[-20.6, -22.4, 28.5, 29.49]])
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(-21, -22, 28, 29)]

    async def test_no_rect_is_noop(self) -> None:
        vac = _make_vacuum()
        await vac.async_clean_zone()
        vac.coordinator.client.start_zone.assert_not_awaited()

    async def test_default_is_world(self) -> None:
        """No `coordinates` parameter = world passthrough, no conversion.

        Uses large positive values that the old pixel contract would have
        transformed; here they must reach start_zone untouched.
        """
        vac = _make_vacuum(state=MagicMock())
        map_data = MagicMock()
        map_data.width = 200
        map_data.height = 271
        map_data.origin_x = -47
        map_data.origin_y = -217
        vac.coordinator.data.map_data = map_data
        await vac.async_clean_zone(zone=[[120, 197, 161, 210]])
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(120, 197, 161, 210)]
        vac.coordinator.client.get_map.assert_not_called()


class TestCoordinatesParamIgnored:
    """The legacy `coordinates` field is accepted but has no effect."""

    def _vac_with_map(self):
        vac = _make_vacuum(state=MagicMock())
        map_data = MagicMock()
        map_data.width = 200
        map_data.height = 271
        map_data.origin_x = -47
        map_data.origin_y = -217
        vac.coordinator.data.map_data = map_data
        return vac

    async def test_coordinates_pixels_no_longer_converts(self) -> None:
        """A pre-2.0 call with coordinates=pixels now passes through as world
        (the pixel transform is gone) and never fetches the map."""
        vac = self._vac_with_map()
        await vac.async_clean_zone(
            zone=[[120, 197, 161, 210]], coordinates="pixels",
        )
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(120, 197, 161, 210)]
        vac.coordinator.client.get_map.assert_not_called()

    async def test_coordinates_auto_ignored(self) -> None:
        vac = self._vac_with_map()
        await vac.async_clean_zone(zone=[[0, 0, 10, 10]], coordinates="auto")
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(0, 0, 10, 10)]
        vac.coordinator.client.get_map.assert_not_called()

    async def test_coordinates_arbitrary_string_accepted(self) -> None:
        """Any string is accepted (schema is cv.string) and ignored."""
        vac = _make_vacuum()
        await vac.async_clean_zone(zone=[[-5, 0, 10, 10]], coordinates="whatever")
        args, _ = vac.coordinator.client.start_zone.await_args
        assert args[0] == [(-5, 0, 10, 10)]
