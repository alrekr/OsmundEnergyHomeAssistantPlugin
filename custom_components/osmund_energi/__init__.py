"""The Osmund Energi integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    KeyDetails,
    Location,
    OsmundApiClient,
    OsmundApiError,
    OsmundAuthError,
)

PLATFORMS: list[Platform] = []


@dataclass(slots=True)
class OsmundRuntimeData:
    """Runtime data stored on a config entry."""

    client: OsmundApiClient
    key_details: KeyDetails
    locations: list[Location]


type OsmundConfigEntry = ConfigEntry[OsmundRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: OsmundConfigEntry) -> bool:
    """Set up Osmund Energi from a config entry."""
    session = async_get_clientsession(hass)
    client = OsmundApiClient(entry.data[CONF_API_KEY], session)

    try:
        key_details = await client.async_get_key_details()
        locations = await client.async_list_locations()
    except OsmundAuthError as err:
        raise ConfigEntryAuthFailed(
            "Osmund Energi API key is invalid, expired, or revoked"
        ) from err
    except OsmundApiError as err:
        raise ConfigEntryNotReady(
            f"Unable to connect to Osmund Energi API: {err}"
        ) from err

    entry.runtime_data = OsmundRuntimeData(
        client=client,
        key_details=key_details,
        locations=locations,
    )

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: OsmundConfigEntry) -> bool:
    """Unload an Osmund Energi config entry."""
    if not PLATFORMS:
        return True
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
