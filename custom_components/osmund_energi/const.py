"""Constants for the Osmund Energi integration."""

from __future__ import annotations

DOMAIN = "osmund_energi"

API_BASE_URL = "https://api.osmundenergi.dk"
DANISH_TZ_NAME = "Europe/Copenhagen"

# Config / options keys
CONF_PRICE_RESOLUTION = "price_resolution"
CONF_REDACT_LOCATION_LABELS = "redact_location_labels"

PRICE_RESOLUTION_AUTO = "auto"
PRICE_RESOLUTION_HOURLY = "PT1H"
PRICE_RESOLUTION_QUARTER_HOURLY = "PT15M"

# Lowercase option identifiers for HA UI selectors / translations
OPTION_RESOLUTION_AUTO = "auto"
OPTION_RESOLUTION_HOURLY = "hourly"
OPTION_RESOLUTION_QUARTER_HOURLY = "quarter_hourly"
PRICE_RESOLUTION_OPTIONS: tuple[str, ...] = (
    OPTION_RESOLUTION_AUTO,
    OPTION_RESOLUTION_HOURLY,
    OPTION_RESOLUTION_QUARTER_HOURLY,
)
OPTION_TO_API_RESOLUTION: dict[str, str] = {
    OPTION_RESOLUTION_HOURLY: PRICE_RESOLUTION_HOURLY,
    OPTION_RESOLUTION_QUARTER_HOURLY: PRICE_RESOLUTION_QUARTER_HOURLY,
    PRICE_RESOLUTION_HOURLY: PRICE_RESOLUTION_HOURLY,
    PRICE_RESOLUTION_QUARTER_HOURLY: PRICE_RESOLUTION_QUARTER_HOURLY,
}
API_TO_OPTION_RESOLUTION: dict[str, str] = {
    PRICE_RESOLUTION_HOURLY: OPTION_RESOLUTION_HOURLY,
    PRICE_RESOLUTION_QUARTER_HOURLY: OPTION_RESOLUTION_QUARTER_HOURLY,
}

DEFAULT_PRICE_RESOLUTION = OPTION_RESOLUTION_AUTO
DEFAULT_REDACT_LOCATION_LABELS = True

# Units & currency
CURRENCY_DKK = "DKK"
UNIT_DKK_PER_KWH = "DKK/kWh"

# Service actions
SERVICE_GET_PRICES = "get_prices"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_LOCATION_ID = "location_id"
ATTR_DAY = "day"
ATTR_RESOLUTION = "resolution"

DAY_TODAY = "today"
DAY_TOMORROW = "tomorrow"
DAY_BOTH = "both"

# API scopes defined by the Osmund API OpenAPI specification
SCOPE_PRICES_READ = "prices:read"
SCOPE_CONSUMPTION_READ = "consumption:read"
SCOPE_COSTS_READ = "costs:read"
SCOPE_LABELS_READ = "labels:read"

# Structured API error codes (always branch on `code`, not `message`)
ERR_UNAUTHORIZED = "unauthorized"
ERR_SCOPE_REQUIRED = "scope_required"
ERR_API_ACCESS_NOT_INCLUDED = "api_access_not_included"
ERR_NOT_FOUND = "not_found"
ERR_INVALID_REQUEST = "invalid_request"
ERR_RATE_LIMITED = "rate_limited"
ERR_TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
ERR_INTERNAL_ERROR = "internal_error"
