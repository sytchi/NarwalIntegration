"""Tests for the clean-mode select entity and its wiring into the vacuum."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.const import (  # noqa: E402
    CLEAN_MODE_LIST,
    DEFAULT_CLEAN_MODE,
)
from custom_components.narwal.select import NarwalCleanModeSelect  # noqa: E402
from custom_components.narwal.vacuum import NarwalVacuum  # noqa: E402
from narwal_client.const import CleanMode  # noqa: E402


def _make_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.clean_mode = DEFAULT_CLEAN_MODE
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_dev_001"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.state = MagicMock()
    coordinator.client.state.firmware_version = "1.0.0"
    coordinator.last_update_success = True
    return coordinator


def _make_select(coordinator: MagicMock) -> NarwalCleanModeSelect:
    sel = NarwalCleanModeSelect.__new__(NarwalCleanModeSelect)
    sel.coordinator = coordinator
    sel.async_write_ha_state = MagicMock()
    return sel


class TestCleanModeSelect:
    def test_options_and_default(self) -> None:
        coordinator = _make_coordinator()
        sel = _make_select(coordinator)
        assert sel.current_option == DEFAULT_CLEAN_MODE
        assert NarwalCleanModeSelect._attr_options == CLEAN_MODE_LIST

    def test_all_four_modes_offered(self) -> None:
        """Vacuum / mop / vacuum+mop / vacuum-then-mop are all selectable."""
        assert CLEAN_MODE_LIST == ["sweep", "mop", "sweep_mop", "sweep_then_mop"]

    async def test_select_option_updates_coordinator(self) -> None:
        coordinator = _make_coordinator()
        sel = _make_select(coordinator)
        await sel.async_select_option("mop")
        assert coordinator.clean_mode == "mop"
        sel.async_write_ha_state.assert_called_once()


class TestVacuumUsesCleanMode:
    def _make_vacuum(self, coordinator: MagicMock) -> NarwalVacuum:
        vac = NarwalVacuum.__new__(NarwalVacuum)
        vac.coordinator = coordinator
        vac._last_fan_speed = None
        vac.last_seen_segments = None
        vac.async_write_ha_state = MagicMock()
        return vac

    async def test_start_uses_start_clean_whole_with_mode(self) -> None:
        """async_start sends the mode via clean/start_clean (start_clean_whole)."""
        coordinator = _make_coordinator()
        coordinator.clean_mode = "sweep"
        coordinator.data = None
        coordinator.client.robot_awake = True
        coordinator.client.start_clean_whole = AsyncMock(
            return_value=MagicMock(result_code=1, success=True)
        )
        coordinator.client.start = AsyncMock()
        vac = self._make_vacuum(coordinator)

        await vac.async_start()
        coordinator.client.start_clean_whole.assert_awaited_once_with(
            clean_mode=CleanMode.SWEEP
        )
        coordinator.client.start.assert_not_awaited()

    async def test_start_falls_back_to_legacy_on_not_applicable(self) -> None:
        """If start_clean is rejected, async_start falls back to start()."""
        from narwal_client.const import CommandResult

        coordinator = _make_coordinator()
        coordinator.clean_mode = "mop"
        coordinator.data = None
        coordinator.client.robot_awake = True
        coordinator.client.start_clean_whole = AsyncMock(
            return_value=MagicMock(
                result_code=CommandResult.NOT_APPLICABLE, success=False
            )
        )
        coordinator.client.start = AsyncMock(
            return_value=MagicMock(result_code=1, success=True)
        )
        vac = self._make_vacuum(coordinator)

        await vac.async_start()
        coordinator.client.start.assert_awaited_once_with(clean_mode=CleanMode.MOP)

    async def test_clean_segments_passes_selected_mode(self) -> None:
        coordinator = _make_coordinator()
        coordinator.clean_mode = "mop"
        coordinator.client.robot_awake = True
        coordinator.client.start_clean_rooms = AsyncMock(
            return_value=MagicMock(result_code=1, success=True)
        )
        coordinator.client.start_rooms = AsyncMock()
        vac = self._make_vacuum(coordinator)

        await vac.async_clean_segments(["2", "4"])
        coordinator.client.start_clean_rooms.assert_awaited_once_with(
            [2, 4], clean_mode=CleanMode.MOP
        )
        coordinator.client.start_rooms.assert_not_awaited()

    def test_unknown_mode_falls_back_to_sweep_mop(self) -> None:
        coordinator = _make_coordinator()
        coordinator.clean_mode = "bogus"
        vac = self._make_vacuum(coordinator)
        assert vac._clean_mode == CleanMode.SWEEP_MOP
