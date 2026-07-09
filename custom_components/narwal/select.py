"""Select entities for Narwal vacuum."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .narwal_client import MopHumidity

from . import NarwalConfigEntry
from .const import CLEAN_MODE_LIST
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)

# robot_base_status field 29 broadcasts the live mop humidity, 1-indexed
# (1=dry, 2=normal/standard, 3=wet). Validated on Flow 2 by the
# StratoGh0st99 fork; on Flow 1 we fall back to the last value we set
# when the field is absent from broadcasts.
_BROADCAST_TO_OPTION: dict[int, str] = {
    1: "dry",
    2: "normal",
    3: "wet",
}

_OPTION_TO_ENUM: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal select entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            NarwalMopHumiditySelect(coordinator),
            NarwalCleanModeSelect(coordinator),
        ]
    )


class NarwalMopHumiditySelect(NarwalEntity, SelectEntity):
    """Select entity for the vacuum's mop humidity setting."""

    _attr_translation_key = "mop_humidity"
    _attr_options = list(_OPTION_TO_ENUM)
    _attr_icon = "mdi:water-percent"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_mop_humidity"
        self._last_set: str | None = None

    @property
    def current_option(self) -> str | None:
        """Return the current mop humidity setting.

        Prefers the live broadcast value (field 29) when the robot
        reports one; otherwise falls back to the last option set from HA.
        """
        state = self.coordinator.data
        raw = None
        if state is not None:
            raw = state.raw_base_status.get("29")
        if raw is not None:
            try:
                return _BROADCAST_TO_OPTION.get(int(raw), self._last_set)
            except (ValueError, TypeError):
                pass
        return self._last_set

    async def async_select_option(self, option: str) -> None:
        """Set the mop humidity."""
        client = self.coordinator.client
        if not client.robot_awake:
            await client.wake(timeout=10.0)
        resp = await client.set_mop_humidity(_OPTION_TO_ENUM[option])
        _LOGGER.debug(
            "set_mop_humidity(%s) response code=%s", option, resp.result_code
        )
        self._last_set = option
        self.async_write_ha_state()


class NarwalCleanModeSelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Clean mode applied to the next clean.

    Options: sweep (vacuum only), mop, sweep_mop (vacuum + mop at the
    same time), sweep_then_mop (vacuum the whole area first, then mop).
    The local protocol has no standalone set-mode command — the mode is
    part of the clean/plan/start payload — so selecting an option only
    stores it on the coordinator; the vacuum entity sends it with the
    next start or room clean.
    """

    _attr_translation_key = "clean_mode"
    _attr_options = CLEAN_MODE_LIST
    _attr_icon = "mdi:broom"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_clean_mode"

    async def async_added_to_hass(self) -> None:
        """Restore the last selected mode across restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in CLEAN_MODE_LIST:
            self.coordinator.clean_mode = last.state

    @property
    def current_option(self) -> str:
        """Return the currently selected clean mode."""
        return self.coordinator.clean_mode

    async def async_select_option(self, option: str) -> None:
        """Store the selected clean mode for the next clean."""
        self.coordinator.clean_mode = option
        self.async_write_ha_state()
