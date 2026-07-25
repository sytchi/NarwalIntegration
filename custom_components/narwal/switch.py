"""Switch entities for Narwal vacuum — map layer visibility toggles."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NarwalLayerSwitchDescription(SwitchEntityDescription):
    """Describes a map layer visibility switch."""

    # Name of the boolean flag on the coordinator this switch controls
    flag: str


SWITCH_DESCRIPTIONS: tuple[NarwalLayerSwitchDescription, ...] = (
    NarwalLayerSwitchDescription(
        key="draw_trail",
        translation_key="draw_trail",
        icon="mdi:chart-line-variant",
        flag="draw_trail",
    ),
    NarwalLayerSwitchDescription(
        key="draw_cleaned_area",
        translation_key="draw_cleaned_area",
        icon="mdi:broom",
        flag="draw_cleaned_area",
    ),
    NarwalLayerSwitchDescription(
        key="draw_furniture",
        translation_key="draw_furniture",
        icon="mdi:sofa-outline",
        flag="draw_furniture",
    ),
    NarwalLayerSwitchDescription(
        key="draw_lidar_walls",
        translation_key="draw_lidar_walls",
        icon="mdi:wall",
        flag="draw_lidar_walls",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal map layer switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        NarwalLayerSwitch(coordinator, description)
        for description in SWITCH_DESCRIPTIONS
    )


class NarwalLayerSwitch(NarwalEntity, SwitchEntity, RestoreEntity):
    """Toggles the visibility of a map camera layer."""

    entity_description: NarwalLayerSwitchDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalLayerSwitchDescription,
    ) -> None:
        """Initialize the layer switch."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    async def async_added_to_hass(self) -> None:
        """Restore the last layer state (defaults to on)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            setattr(
                self.coordinator, self.entity_description.flag, last.state == "on",
            )

    @property
    def is_on(self) -> bool:
        """Return True when the layer is drawn."""
        return getattr(self.coordinator, self.entity_description.flag, True)

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the layer and refresh the map cameras."""
        setattr(self.coordinator, self.entity_description.flag, True)
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the layer and refresh the map cameras."""
        setattr(self.coordinator, self.entity_description.flag, False)
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()
