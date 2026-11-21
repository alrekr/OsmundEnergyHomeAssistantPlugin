"""The Osmund Energi integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    KeyDetails,
    OsmundApiClient,
    OsmundApiError,
    OsmundAuthError,
)
from .const import DOMAIN
from .coordinator import (
    OsmundAccountCoordinator,
    OsmundPriceCoordinator,
    RateLimitGate,
)
from .services import async_register_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass(slots=True)
class OsmundRuntimeData:
    """Runtime data stored on a config entry."""

    client: OsmundApiClient
    key_details: KeyDetails
    rate_limit_gate: RateLimitGate
    account_coordinator: OsmundAccountCoordinator
    price_coordinator: OsmundPriceCoordinator


type OsmundConfigEntry = ConfigEntry[OsmundRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Osmund Energi domain services."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OsmundConfigEntry) -> bool:
    """Set up Osmund Energi from a config entry."""
    async_register_services(hass)

    session = async_get_clientsession(hass)
    client = OsmundApiClient(entry.data[CONF_API_KEY], session)

    try:
        key_details = await client.async_get_key_details()
    except OsmundAuthError as err:
        raise ConfigEntryAuthFailed(
            "Osmund Energi API key is invalid, expired, or revoked"
        ) from err
    except OsmundApiError as err:
        raise ConfigEntryNotReady(
            f"Unable to connect to Osmund Energi API: {err}"
        ) from err

    rate_limit_gate = RateLimitGate()
    account_coordinator = OsmundAccountCoordinator(
        hass,
        entry,
        client,
        key_details,
        rate_limit_gate,
    )
    await account_coordinator.async_config_entry_first_refresh()

    price_coordinator = OsmundPriceCoordinator(
        hass,
        entry,
        client,
        account_coordinator,
        rate_limit_gate,
    )
    await price_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = OsmundRuntimeData(
        client=client,
        key_details=key_details,
        rate_limit_gate=rate_limit_gate,
        account_coordinator=account_coordinator,
        price_coordinator=price_coordinator,
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: OsmundConfigEntry
) -> None:
    """Reload the integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OsmundConfigEntry) -> bool:
    """Unload an Osmund Energi config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
