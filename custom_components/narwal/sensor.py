"""Sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import ERROR_CODE_SLUGS, ERROR_HELP_URL_TEMPLATE
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import NarwalState, WorkingStatus


@dataclass(frozen=True, kw_only=True)
class NarwalSensorEntityDescription(SensorEntityDescription):
    """Describes a Narwal sensor entity."""

    value_fn: Callable[[NarwalState], float | str | None]


SENSOR_DESCRIPTIONS: tuple[NarwalSensorEntityDescription, ...] = (
    NarwalSensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # battery_level comes from field 2 (real-time SOC as float32)
        value_fn=lambda state: state.battery_level if state.battery_level > 0 else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_area",
        translation_key="cleaning_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 13 is cm²; divide by 10000 for m².
        # NEEDS LIVE VALIDATION: only populated during active cleaning.
        value_fn=lambda state: round(state.cleaning_area / 10000, 2)
        if state.cleaning_area > 0
        else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_time",
        translation_key="cleaning_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 3 is session elapsed seconds.
        # NEEDS LIVE VALIDATION: only populated during active cleaning.
        value_fn=lambda state: state.cleaning_time
        if state.cleaning_time > 0
        else None,
    ),
    NarwalSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.firmware_version or None,
    ),
    NarwalSensorEntityDescription(
        key="dust_bag_health",
        translation_key="dust_bag_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:trash-can-outline",
        # base_status field 41: 100 = healthy/empty bag, drops as it fills.
        value_fn=lambda state: state.dust_bag_health
        if state.dust_bag_health > 0
        else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_progress",
        translation_key="cleaning_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-check",
        # working_status field 1 holds the last task's percent while
        # idle — only report during an active clean.
        value_fn=lambda state: round(state.cleaning_progress_pct, 1)
        if state.is_cleaning
        else None,
    ),
    NarwalSensorEntityDescription(
        key="station_activity",
        translation_key="station_activity",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "mop_washing", "mop_drying", "dust_emptying"],
        icon="mdi:home-import-outline",
        value_fn=lambda state: (
            "dust_emptying"
            if state.station_dust_emptying
            else "mop_drying"
            if state.station_mop_drying or state.mop_drying_target > 0
            else "mop_washing"
            if state.working_status == WorkingStatus.MOP_WASHING
            else "idle"
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal sensor entities."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        NarwalSensor(coordinator, description) for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(NarwalChargingStateSensor(coordinator))
    entities.append(NarwalErrorSensor(coordinator))
    async_add_entities(entities)


class NarwalSensor(NarwalEntity, SensorEntity):
    """A Narwal sensor entity."""

    entity_description: NarwalSensorEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> float | str | None:
        """Return the sensor value."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.value_fn(state)


class NarwalErrorSensor(NarwalEntity, SensorEntity):
    """Active fault reported by the robot.

    State is "no_error", a translation slug for known fault codes
    (ERROR_CODE_SLUGS), or the numeric fault code for unknown ones.
    The numeric code stays available as the "code" attribute (the
    firmware's message arrives in its own locale, so the code is the
    stable key for automations).
    """

    _attr_translation_key = "error"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the error sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_error"

    @property
    def native_value(self) -> str | None:
        """Return no_error, a known-fault slug, or the raw code."""
        state = self.coordinator.data
        if state is None:
            return None
        if not state.error_code:
            return "no_error"
        return ERROR_CODE_SLUGS.get(state.error_code, str(state.error_code))

    @property
    def extra_state_attributes(self) -> dict[str, str | int] | None:
        """Return error details."""
        state = self.coordinator.data
        if state is None or not state.error_code:
            return None
        return {
            "code": state.error_code,
            "code_hex": f"0x{state.error_code:08X}",
            "message": state.error_message,
            "severity": state.error_severity,
            "help_url": ERROR_HELP_URL_TEMPLATE.format(
                code=f"{state.error_code:08x}"
            ),
        }

    @property
    def icon(self) -> str:
        """Return icon based on error state."""
        if self.native_value in (None, "no_error"):
            return "mdi:check-circle-outline"
        return "mdi:alert-circle"


class NarwalChargingStateSensor(NarwalEntity, SensorEntity):
    """Sensor showing charging state: Charging, Fully Charged, or unavailable."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "charging_state"
    _attr_options = ["charging", "fully_charged", "not_charging"]

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the charging state sensor."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_charging_state"

    @property
    def native_value(self) -> str | None:
        """Return charging state.

        Returns None (unavailable) when not docked.
        """
        state = self.coordinator.data
        if state is None:
            return None
        if not state.is_docked:
            return "not_charging"
        if state.battery_level >= 100:
            return "fully_charged"
        return "charging"

    @property
    def icon(self) -> str:
        """Return icon based on charging state."""
        if self.native_value == "fully_charged":
            return "mdi:battery"
        if self.native_value == "charging":
            return "mdi:battery-charging"
        if self.native_value == "not_charging":
            return "mdi:battery-off-outline"
        return "mdi:battery-unknown"
