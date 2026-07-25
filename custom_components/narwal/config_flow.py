"""Config flow for Narwal vacuum integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_MAP_SCALE,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DEFAULT_MAP_SCALE,
    DEFAULT_PORT,
    DOMAIN,
    MAX_MAP_SCALE,
    MIN_MAP_SCALE,
    NARWAL_MODELS,
)
from .narwal_client import NarwalClient

_LOGGER = logging.getLogger(__name__)

MODEL_OPTIONS = list(NARWAL_MODELS.keys())

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
        vol.Required(CONF_MODEL, default=MODEL_OPTIONS[0]): vol.In(MODEL_OPTIONS),
    }
)


class NarwalOptionsFlow(OptionsFlow):
    """Handle Narwal options (map rendering scale)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MAP_SCALE,
                    default=self.config_entry.options.get(
                        CONF_MAP_SCALE, DEFAULT_MAP_SCALE
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_MAP_SCALE, max=MAX_MAP_SCALE),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Narwal vacuum."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NarwalOptionsFlow:
        """Create the options flow."""
        return NarwalOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — user enters IP, port, and model."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            port = user_input.get("port", DEFAULT_PORT)
            model_label = user_input[CONF_MODEL]
            product_key = NARWAL_MODELS[model_label]

            # If user selected a specific model, set topic prefix directly
            topic_prefix = None if product_key == "auto" else f"/{product_key}"

            client = NarwalClient(
                host=host, port=port, topic_prefix=topic_prefix,
            )
            try:
                await client.connect()
                # Discover device_id from broadcast, then query info
                await client.discover_device_id(timeout=15.0)
                # Drain any stale field5 responses left in the WebSocket
                # buffer from discover's wake probes before sending a
                # real command
                await client.drain_ws_buffer()
                device_info = await client.get_device_info()
            except Exception as ex:
                _LOGGER.warning(
                    "Setup failed: %s: %s", type(ex).__name__, ex,
                )
                errors["base"] = "cannot_connect"
            else:
                device_id = device_info.device_id
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()

                # Use the product key that actually worked (may have been
                # auto-detected during discovery even if user picked "auto")
                resolved_key = client.topic_prefix.lstrip("/")

                return self.async_create_entry(
                    title=model_label if product_key != "auto" else f"Narwal {resolved_key}",
                    data={
                        "host": host,
                        "port": port,
                        "device_id": device_id,
                        CONF_PRODUCT_KEY: resolved_key,
                        CONF_MODEL: model_label,
                    },
                )
            finally:
                await client.disconnect()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
