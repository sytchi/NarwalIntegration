"""Tests for NarwalVacuum.activity mapping edge cases.

Covers the fw v01.08.03+ point-navi shape: working_status stays DOCKED_V2
while the robot drives off dock (dock fields 11=1 / 47=2), so the entity
must report CLEANING, not DOCKED.
"""

from __future__ import annotations

from unittest.mock import MagicMock

# Install HA stubs before any custom_components import
import tests.ha_stubs  # noqa: E402

tests.ha_stubs.install()

from homeassistant.components.vacuum import VacuumActivity  # noqa: E402

from narwal_client.models import NarwalState  # noqa: E402
from custom_components.narwal.vacuum import NarwalVacuum  # noqa: E402


def _make_vacuum(state: NarwalState | None) -> NarwalVacuum:
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"device_id": "test_dev_001"}
    coordinator.config_entry.title = "Narwal Test"
    coordinator.last_update_success = True

    vac = NarwalVacuum.__new__(NarwalVacuum)
    vac.coordinator = coordinator
    vac._attr_unique_id = "test_dev_001"
    vac._attr_device_info = {}
    vac._last_fan_speed = None
    return vac


class TestActivityDockedV2OffDock:
    def test_point_navi_off_dock_reports_cleaning(self) -> None:
        """DOCKED_V2 + explicit off-dock dock fields → CLEANING."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 2, "4": 3},
            "11": 1, "47": 2,
        })
        vac = _make_vacuum(state)
        assert vac.activity == VacuumActivity.CLEANING

    def test_docked_v2_on_dock_reports_docked(self) -> None:
        """DOCKED_V2 with docked field values still reports DOCKED."""
        state = NarwalState()
        state.update_from_base_status({
            "3": {"1": 2, "4": 1, "11": 3},
            "11": 3, "47": 1,
        })
        vac = _make_vacuum(state)
        assert vac.activity == VacuumActivity.DOCKED

    def test_docked_v2_no_dock_fields_reports_docked(self) -> None:
        """DOCKED_V2 with absent dock fields (asleep) reports DOCKED."""
        state = NarwalState()
        state.update_from_base_status({"3": {"1": 2}})
        vac = _make_vacuum(state)
        assert vac.activity == VacuumActivity.DOCKED
