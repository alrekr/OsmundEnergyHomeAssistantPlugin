"""Config and options flows for the Osmund Energi integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    KeyDetails,
    Location,
    OsmundApiClient,
    OsmundApiError,
    OsmundAuthError,
    OsmundConnectionError,
    OsmundRateLimitError,
)
from .const import (
    CONF_PRICE_RESOLUTION,
    CONF_REDACT_LOCATION_LABELS,
    DEFAULT_PRICE_RESOLUTION,
    DEFAULT_REDACT_LOCATION_LABELS,
    DOMAIN,
    PRICE_RESOLUTION_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(
            CONF_REDACT_LOCATION_LABELS,
            default=DEFAULT_REDACT_LOCATION_LABELS,
        ): BooleanSelector(),
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class OsmundEnergiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Osmund Energi."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OsmundEnergiOptionsFlow:
        """Return the options flow for Osmund Energi."""
        return OsmundEnergiOptionsFlow()

    async def _async_validate_api_key(
        self, api_key: str
    ) -> tuple[KeyDetails, list[Location]]:
        """Validate the API key and fetch account locations."""
        session = async_get_clientsession(self.hass)
        client = OsmundApiClient(api_key, session)
        key_details = await client.async_get_key_details()
        locations = await client.async_list_locations()
        return key_details, locations

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step where the user enters their Osmund API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            redact_labels = bool(
                user_input.get(
                    CONF_REDACT_LOCATION_LABELS, DEFAULT_REDACT_LOCATION_LABELS
                )
            )
            try:
                key_details, locations = await self._async_validate_api_key(api_key)
            except OsmundAuthError:
                errors["base"] = "invalid_auth"
            except (OsmundConnectionError, OsmundRateLimitError, OsmundApiError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating Osmund Energi API key")
                errors["base"] = "unknown"
            else:
                if not any(loc.has_api_access for loc in locations):
                    errors["base"] = "no_api_access"
                else:
                    await self.async_set_unique_id(key_details.prefix)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Osmund Energi ({key_details.name})",
                        data={CONF_API_KEY: api_key},
                        options={
                            CONF_REDACT_LOCATION_LABELS: redact_labels,
                            CONF_PRICE_RESOLUTION: DEFAULT_PRICE_RESOLUTION,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            description_placeholders={"api_url": "https://osmundenergi.dk/min-side#api"},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauthentication with a new Osmund Energi API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            try:
                key_details, locations = await self._async_validate_api_key(api_key)
            except OsmundAuthError:
                errors["base"] = "invalid_auth"
            except (OsmundConnectionError, OsmundRateLimitError, OsmundApiError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Osmund Energi reauth")
                errors["base"] = "unknown"
            else:
                if not any(loc.has_api_access for loc in locations):
                    errors["base"] = "no_api_access"
                else:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        unique_id=key_details.prefix,
                        data_updates={CONF_API_KEY: api_key},
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            errors=errors,
        )


class OsmundEnergiOptionsFlow(OptionsFlow):
    """Handle Osmund Energi integration options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage Osmund Energi options."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_PRICE_RESOLUTION: user_input[CONF_PRICE_RESOLUTION],
                    CONF_REDACT_LOCATION_LABELS: bool(
                        user_input[CONF_REDACT_LOCATION_LABELS]
                    ),
                },
            )

        current_resolution = self.config_entry.options.get(
            CONF_PRICE_RESOLUTION, DEFAULT_PRICE_RESOLUTION
        )
        current_redact = self.config_entry.options.get(
            CONF_REDACT_LOCATION_LABELS, DEFAULT_REDACT_LOCATION_LABELS
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PRICE_RESOLUTION,
                    default=current_resolution,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(PRICE_RESOLUTION_OPTIONS),
                        translation_key="price_resolution",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_REDACT_LOCATION_LABELS,
                    default=current_redact,
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
