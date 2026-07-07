"""Button entities for Narwal vacuum."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .narwal_client.client import NarwalClient
from .narwal_client.const import CommandResult

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NarwalButtonEntityDescription(ButtonEntityDescription):
    """Describes a Narwal button entity."""

    press_fn: Callable[[NarwalClient], Awaitable[Any]]
    # Wake the robot before sending — all station commands need the
    # application CPU awake, but the wake button IS the wake action.
    wake_first: bool = True


BUTTON_DESCRIPTIONS: tuple[NarwalButtonEntityDescription, ...] = (
    NarwalButtonEntityDescription(
        key="wash_mop",
        translation_key="wash_mop",
        icon="mdi:water-sync",
        press_fn=lambda client: client.wash_mop(),
    ),
    NarwalButtonEntityDescription(
        key="dry_mop",
        translation_key="dry_mop",
        icon="mdi:hair-dryer",
        press_fn=lambda client: client.dry_mop(),
    ),
    NarwalButtonEntityDescription(
        key="empty_dustbin",
        translation_key="empty_dustbin",
        icon="mdi:delete-empty",
        press_fn=lambda client: client.empty_dustbin(),
    ),
    NarwalButtonEntityDescription(
        key="wake",
        translation_key="wake",
        icon="mdi:sleep-off",
        press_fn=lambda client: client.wake(force=True),
        wake_first=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal button entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        NarwalButton(coordinator, description) for description in BUTTON_DESCRIPTIONS
    )


class NarwalButton(NarwalEntity, ButtonEntity):
    """Button entity that sends a one-shot command to the vacuum."""

    entity_description: NarwalButtonEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        device_id = coordinator.config_entry.data["device_id"]
        self._attr_unique_id = f"{device_id}_{description.key}"

    async def async_press(self) -> None:
        """Handle the button press."""
        client = self.coordinator.client
        if self.entity_description.wake_first and not client.robot_awake:
            _LOGGER.debug("Robot not awake — sending wake burst before %s", self.entity_description.key)
            await client.wake(timeout=10.0)
        resp = await self.entity_description.press_fn(client)
        result = getattr(resp, "result_code", None)
        if result is not None and result != CommandResult.SUCCESS:
            try:
                result_name = CommandResult(result).name
            except ValueError:
                result_name = str(result)
            _LOGGER.warning(
                "Button %s command returned %s (code=%s)",
                self.entity_description.key,
                result_name,
                result,
            )
