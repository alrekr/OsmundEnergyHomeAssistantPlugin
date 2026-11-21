"""Service actions for the Osmund Energi integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import OsmundApiError
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DAY,
    ATTR_LOCATION_ID,
    ATTR_RESOLUTION,
    DAY_BOTH,
    DAY_TODAY,
    DAY_TOMORROW,
    DOMAIN,
    OPTION_RESOLUTION_HOURLY,
    OPTION_RESOLUTION_QUARTER_HOURLY,
    OPTION_TO_API_RESOLUTION,
    PRICE_RESOLUTION_HOURLY,
    PRICE_RESOLUTION_QUARTER_HOURLY,
    SCOPE_PRICES_READ,
    SERVICE_GET_PRICES,
)

SERVICE_GET_PRICES_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_LOCATION_ID): cv.string,
        vol.Optional(ATTR_DAY, default=DAY_TODAY): vol.In(
            [DAY_TODAY, DAY_TOMORROW, DAY_BOTH]
        ),
        vol.Optional(ATTR_RESOLUTION): vol.In(
            [
                OPTION_RESOLUTION_HOURLY,
                OPTION_RESOLUTION_QUARTER_HOURLY,
                PRICE_RESOLUTION_HOURLY,
                PRICE_RESOLUTION_QUARTER_HOURLY,
            ]
        ),
    }
)


def _resolve_config_entry(hass: HomeAssistant, entry_id: str | None) -> Any:
    """Resolve the target Osmund Energi config entry."""
    loaded_entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded_entries:
        raise ServiceValidationError(
            "No loaded Osmund Energi integration entries found"
        )

    if entry_id is not None:
        for entry in loaded_entries:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(
            f"Osmund Energi config entry '{entry_id}' is not loaded or does not exist"
        )

    if len(loaded_entries) > 1:
        raise ServiceValidationError(
            "Multiple Osmund Energi entries are configured; specify config_entry_id"
        )
    return loaded_entries[0]


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register Osmund Energi domain services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_PRICES):
        return

    async def _async_handle_get_prices(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_config_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        runtime = entry.runtime_data
        account_coordinator = runtime.account_coordinator
        price_coordinator = runtime.price_coordinator

        if not account_coordinator.key_details.has_scope(SCOPE_PRICES_READ):
            raise ServiceValidationError(
                "This Osmund Energi API key does not have the 'prices:read' scope"
            )

        open_locations = account_coordinator.open_locations
        if not open_locations:
            raise ServiceValidationError(
                "No Osmund Energi locations with active API access were found"
            )

        location_id: str | None = call.data.get(ATTR_LOCATION_ID)
        if location_id is not None:
            location = next(
                (loc for loc in open_locations if loc.id == location_id), None
            )
            if location is None:
                raise ServiceValidationError(
                    f"Location '{location_id}' was not found or has no API access"
                )
        elif len(open_locations) == 1:
            location = open_locations[0]
        else:
            raise ServiceValidationError(
                "Multiple locations have API access; specify location_id"
            )

        day: str = call.data[ATTR_DAY]
        raw_resolution: str | None = call.data.get(ATTR_RESOLUTION)
        requested_resolution: str | None = (
            OPTION_TO_API_RESOLUTION[raw_resolution]
            if raw_resolution is not None
            else None
        )

        try:
            resolution, days = await price_coordinator.async_get_prices_for_service(
                location,
                day,
                requested_resolution,
            )
        except OsmundApiError as err:
            raise HomeAssistantError(
                f"Failed to fetch Osmund Energi prices: {err}"
            ) from err

        return {
            "location_id": location.id,
            "price_area": location.price_area,
            "currency": "DKK",
            "unit": "kr/kWh",
            "resolution": resolution,
            "days": {
                date_str: price_day.to_service_dict()
                for date_str, price_day in days.items()
            },
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PRICES,
        _async_handle_get_prices,
        schema=SERVICE_GET_PRICES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
