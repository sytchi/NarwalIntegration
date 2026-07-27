"""Tests for surfacing rejected robot commands.

A rejected start used to be logged and swallowed, so a map-card tap or an
automation step looked successful while the robot never moved. Start, room
clean and zone clean now raise HomeAssistantError with the reason; resume
stays log-only because it is sent blind by design.

Result code 4 (NOT_READY) is the code the robot returns when it declines to
start a job it could otherwise run — observed on fw v01.08.03 right after a
long clean: rejected at 23-26% battery, accepted at 30%.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.narwal.vacuum import (  # noqa: E402
    NarwalVacuum,
    describe_command_result,
)
from narwal_client.const import CommandResult  # noqa: E402
from narwal_client.models import NarwalState  # noqa: E402


def _resp(code: int, success: bool = False) -> MagicMock:
    """Build a command response with the given result code."""
    return MagicMock(result_code=code, success=success)


def _make_vacuum(state: NarwalState | None = None) -> NarwalVacuum:
    """Create a NarwalVacuum with mocked coordinator and client."""
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_dev_001"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.clean_mode = "sweep"
    coordinator.client = MagicMock()
    coordinator.client.robot_awake = True
    coordinator.last_update_success = True

    vac = NarwalVacuum.__new__(NarwalVacuum)
    vac.coordinator = coordinator
    vac._attr_unique_id = "test_dev_001"
    vac._attr_device_info = {}
    vac._last_fan_speed = None
    vac.async_write_ha_state = MagicMock()
    return vac


class TestDescribeCommandResult:
    """Rendering of result codes into readable text."""

    def test_known_code_gets_name_and_hint(self) -> None:
        text = describe_command_result(_resp(CommandResult.CONFLICT))
        assert "CONFLICT" in text
        assert "code=3" in text
        assert "busy" in text

    def test_not_ready_explains_charging(self) -> None:
        text = describe_command_result(_resp(CommandResult.NOT_READY))
        assert "NOT_READY" in text
        assert "code=4" in text
        assert "charge" in text

    def test_unknown_code_still_renders(self) -> None:
        text = describe_command_result(_resp(99))
        assert "UNKNOWN(99)" in text
        assert "code=99" in text


class TestZoneCleanErrors:
    """async_clean_zone raises on rejection."""

    async def test_raises_on_not_ready_with_battery(self) -> None:
        state = NarwalState()
        state.battery_level = 23
        vac = _make_vacuum(state)
        vac.coordinator.client.start_zone = AsyncMock(
            return_value=_resp(CommandResult.NOT_READY)
        )

        with pytest.raises(HomeAssistantError) as err:
            await vac.async_clean_zone(zone=[[-17, -72, 25, 25]])

        message = str(err.value)
        assert "Zone clean failed" in message
        assert "NOT_READY" in message
        assert "battery is at 23%" in message

    async def test_no_battery_in_message_without_state(self) -> None:
        vac = _make_vacuum(state=None)
        vac.coordinator.client.start_zone = AsyncMock(
            return_value=_resp(CommandResult.NOT_READY)
        )

        with pytest.raises(HomeAssistantError) as err:
            await vac.async_clean_zone(zone=[[-17, -72, 25, 25]])

        assert "battery is at" not in str(err.value)

    async def test_success_does_not_raise(self) -> None:
        vac = _make_vacuum(state=None)
        vac.coordinator.client.start_zone = AsyncMock(
            return_value=_resp(CommandResult.SUCCESS, success=True)
        )
        await vac.async_clean_zone(zone=[[-17, -72, 25, 25]])


class TestRoomCleanErrors:
    """Room clean raises on rejection, after the legacy fallback."""

    async def test_raises_when_both_paths_fail(self) -> None:
        vac = _make_vacuum(state=None)
        vac.coordinator.client.start_clean_rooms = AsyncMock(
            return_value=_resp(CommandResult.NOT_APPLICABLE)
        )
        vac.coordinator.client.start_rooms = AsyncMock(
            return_value=_resp(CommandResult.NOT_READY)
        )

        with pytest.raises(HomeAssistantError) as err:
            await vac.async_clean_rooms(rooms=[1])

        vac.coordinator.client.start_rooms.assert_awaited_once()
        assert "Room clean failed" in str(err.value)

    async def test_success_does_not_raise(self) -> None:
        vac = _make_vacuum(state=None)
        vac.coordinator.client.start_clean_rooms = AsyncMock(
            return_value=_resp(CommandResult.SUCCESS, success=True)
        )
        await vac.async_clean_rooms(rooms=[1, 2])


class TestStartErrors:
    """vacuum.start raises on rejection."""

    async def test_raises_on_not_ready(self) -> None:
        state = NarwalState()
        state.battery_level = 26
        vac = _make_vacuum(state)
        vac.coordinator.client.start_clean_whole = AsyncMock(
            return_value=_resp(CommandResult.NOT_READY)
        )

        with pytest.raises(HomeAssistantError) as err:
            await vac.async_start()

        message = str(err.value)
        assert "Start failed" in message
        assert "battery is at 26%" in message

    async def test_success_does_not_raise(self) -> None:
        vac = _make_vacuum(state=None)
        vac.coordinator.client.start_clean_whole = AsyncMock(
            return_value=_resp(CommandResult.SUCCESS, success=True)
        )
        await vac.async_start()


class TestResumeStaysQuiet:
    """narwal.resume is sent blind — a rejection must not raise."""

    async def test_rejected_resume_does_not_raise(self) -> None:
        vac = _make_vacuum(state=None)
        vac.coordinator.client.resume = AsyncMock(
            return_value=_resp(CommandResult.NOT_APPLICABLE)
        )
        await vac.async_resume_task()
