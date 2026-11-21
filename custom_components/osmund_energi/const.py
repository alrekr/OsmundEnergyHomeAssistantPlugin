"""Constants for the Osmund Energi integration."""

from __future__ import annotations

DOMAIN = "osmund_energi"

API_BASE_URL = "https://api.osmundenergi.dk"

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
