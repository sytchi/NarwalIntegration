"""Tests for the narwal.resume entity service (async_resume_task).

The service sends task/resume unconditionally — unlike vacuum.start, which
only resumes when the coordinator reports CLEANING+paused. During real stuck
events the broadcast state can lag behind (e.g. still "docked"), so the
service must not gate on the reported state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from custom_components.narwal.vacuum import NarwalVacuum  # noqa: E402
from narwal_client.const import WorkingStatus  # noqa: E402
from narwal_client.models import NarwalState  # noqa: E402


def _make_vacuum(state: NarwalState | None = None) -> NarwalVacuum:
    """Create a NarwalVacuum with mocked coordinator."""
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_dev_001"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.client = MagicMock()
    coordinator.client.robot_awake = True
    coordinator.client.resume = AsyncMock(
        return_value=MagicMock(result_code=0, success=True)
    )
    coordinator.last_update_success = True

    vac = NarwalVacuum.__new__(NarwalVacuum)
    vac.coordinator = coordinator
    vac._attr_unique_id = "test_dev_001"
    vac._attr_device_info = {}
    vac._last_fan_speed = None
    vac.async_write_ha_state = MagicMock()

    return vac


class TestAsyncResumeTask:
    """Tests for async_resume_task."""

    async def test_resumes_without_any_state(self) -> None:
        """Sends task/resume even when coordinator.data is None."""
        vac = _make_vacuum(state=None)
        await vac.async_resume_task()
        vac.coordinator.client.resume.assert_awaited_once_with(
            timeout=NarwalVacuum._ACTION_TIMEOUT
        )

    async def test_resumes_when_state_reports_docked(self) -> None:
        """Sends task/resume even when the (lagging) state says docked.

        This is the exact 2026-07-14 incident: robot stuck on a doormat with
        a "robot lifted" error while the entity still reported docked, so the
        vacuum.start resume path never fired.
        """
        state = NarwalState()
        state.working_status = WorkingStatus.DOCKED
        vac = _make_vacuum(state=state)
        await vac.async_resume_task()
        vac.coordinator.client.resume.assert_awaited_once()

    async def test_resumes_when_paused(self) -> None:
        """Sends task/resume in the normal paused case too."""
        state = NarwalState()
        state.working_status = WorkingStatus.CLEANING
        state.is_paused = True
        vac = _make_vacuum(state=state)
        await vac.async_resume_task()
        vac.coordinator.client.resume.assert_awaited_once()

    async def test_wakes_robot_before_resume(self) -> None:
        """Sends a wake burst first when the robot is not broadcasting."""
        vac = _make_vacuum(state=None)
        vac.coordinator.client.robot_awake = False
        vac.coordinator.client.wake = AsyncMock(return_value=False)
        await vac.async_resume_task()
        vac.coordinator.client.wake.assert_awaited_once()
        vac.coordinator.client.resume.assert_awaited_once()

    async def test_unsuccessful_resume_does_not_raise(self) -> None:
        """A rejected resume (no task to resume) logs but does not raise."""
        vac = _make_vacuum(state=None)
        vac.coordinator.client.resume = AsyncMock(
            return_value=MagicMock(result_code=2, success=False)
        )
        await vac.async_resume_task()
        vac.coordinator.client.resume.assert_awaited_once()


class TestAsyncStartResume:
    """The play button (async_start) must resume a paused task, not restart.

    On fw v01.08.03+ a paused clean reports working_status=DOCKED_V2(2), not
    CLEANING(4). The resume gate must recognize that shape, otherwise the play
    button sends start_clean_whole (a fresh whole-house clean) over a paused
    task.
    """

    def _vac_with_start_mocks(self, state: NarwalState) -> NarwalVacuum:
        vac = _make_vacuum(state=state)
        vac.coordinator.client.start_clean_whole = AsyncMock(
            return_value=MagicMock(result_code=0, success=True)
        )
        vac.coordinator.client.start = AsyncMock(
            return_value=MagicMock(result_code=0, success=True)
        )
        vac.coordinator.clean_mode = "sweep_mop"
        return vac

    async def test_start_resumes_paused_docked_v2_off_dock(self) -> None:
        """Paused DOCKED_V2 off dock → play button resumes, no new clean."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 2, "2": 1},
            "11": 1, "47": 2,
        })
        vac = self._vac_with_start_mocks(state)
        await vac.async_start()
        vac.coordinator.client.resume.assert_awaited_once()
        vac.coordinator.client.start_clean_whole.assert_not_awaited()

    async def test_start_starts_new_clean_when_not_paused(self) -> None:
        """Cleaning (not paused) → play button starts a clean, not resume."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 2},
            "11": 1, "47": 2,
        })
        vac = self._vac_with_start_mocks(state)
        await vac.async_start()
        vac.coordinator.client.resume.assert_not_awaited()
        vac.coordinator.client.start_clean_whole.assert_awaited_once()
