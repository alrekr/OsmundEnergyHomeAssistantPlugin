"""Async client for the Osmund Energi API.

This module is intentionally Home Assistant agnostic so it can be tested
directly in scripts/unit tests and extracted into a standalone PyPI package if
this integration moves to Home Assistant Core.

OpenAPI specification (v1.1.0-beta):
https://osmundenergi.dk/docs/osmund-api/openapi.yaml
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import (
    API_BASE_URL,
    ERR_API_ACCESS_NOT_INCLUDED,
    ERR_RATE_LIMITED,
    ERR_SCOPE_REQUIRED,
    ERR_TEMPORARILY_UNAVAILABLE,
    ERR_UNAUTHORIZED,
)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)


class OsmundApiError(Exception):
    """Base exception for Osmund Energi API errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id


class OsmundAuthError(OsmundApiError):
    """Raised when the API key is missing, invalid, expired, or revoked (401)."""


class OsmundScopeError(OsmundApiError):
    """Raised when the API key lacks the required scope for a route (403)."""

    def __init__(
        self,
        message: str,
        *,
        scope: str | None = None,
        code: str | None = None,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, status=status, request_id=request_id)
        self.scope = scope


class OsmundAccessError(OsmundApiError):
    """Raised when API access (Osmund Plus) is not included at a location (403)."""

    def __init__(
        self,
        message: str,
        *,
        location_id: str | None = None,
        code: str | None = None,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, status=status, request_id=request_id)
        self.location_id = location_id


class OsmundRateLimitError(OsmundApiError):
    """Raised when the API rate limit is exceeded (429)."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        code: str | None = None,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, status=status, request_id=request_id)
        self.retry_after = retry_after


class OsmundConnectionError(OsmundApiError):
    """Raised on network errors, timeouts, or 503 temporary unavailability."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        code: str | None = None,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, status=status, request_id=request_id)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class RateLimitInfo:
    """Rate-limit metadata returned in response headers."""

    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None


@dataclass(frozen=True, slots=True)
class KeyDetails:
    """Metadata about the active API key from GET /api/v1/me."""

    name: str
    prefix: str
    scopes: tuple[str, ...]
    expires_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyDetails:
        key = data.get("key", data)
        return cls(
            name=str(key["name"]),
            prefix=str(key["prefix"]),
            scopes=tuple(str(s) for s in key.get("scopes", ())),
            expires_at=str(key["expiresAt"]),
        )


@dataclass(frozen=True, slots=True)
class LocationPlus:
    """Osmund Plus subscription status for a location."""

    status: str
    trial_ends_on: str | None
    ends_on: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LocationPlus:
        return cls(
            status=str(data.get("status", "unknown")),
            trial_ends_on=data.get("trialEndsOn"),
            ends_on=data.get("endsOn"),
        )


@dataclass(frozen=True, slots=True)
class Location:
    """An electricity supply location from GET /api/v1/locations."""

    id: str
    label: str | None
    price_area: str | None
    state: str
    plan: str | None
    api_access: str
    plus: LocationPlus
    consumption_through: str | None
    meter_resolution: str | None
    rated_through: str | None

    @property
    def has_api_access(self) -> bool:
        """Return True when the API serves routes for this location."""
        return self.api_access == "open"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Location:
        return cls(
            id=str(data["id"]),
            label=data.get("label"),
            price_area=data.get("priceArea"),
            state=str(data.get("state", "unknown")),
            plan=data.get("plan"),
            api_access=str(data.get("apiAccess", "closed")),
            plus=LocationPlus.from_dict(data.get("plus") or {}),
            consumption_through=data.get("consumptionThrough"),
            meter_resolution=data.get("meterResolution"),
            rated_through=data.get("ratedThrough"),
        )


def _parse_int_header(headers: Any, name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class OsmundApiClient:
    """Async HTTP client for the Osmund Energi REST API."""

    def __init__(
        self,
        api_key: str,
        session: aiohttp.ClientSession,
        *,
        base_url: str = API_BASE_URL,
        timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        self._api_key = api_key.strip()
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.last_rate_limit: RateLimitInfo | None = None

    def __repr__(self) -> str:
        # Never expose the full API key in repr/logs.
        return f"OsmundApiClient(base_url={self._base_url!r}, api_key='***')"

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        try:
            async with self._session.get(
                url,
                headers=headers,
                params=params,
                timeout=self._timeout,
            ) as response:
                self.last_rate_limit = RateLimitInfo(
                    limit=_parse_int_header(response.headers, "RateLimit-Limit"),
                    remaining=_parse_int_header(
                        response.headers, "RateLimit-Remaining"
                    ),
                    reset=_parse_int_header(response.headers, "RateLimit-Reset"),
                )

                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    payload = {}

                if response.ok:
                    if not isinstance(payload, dict):
                        raise OsmundApiError(
                            "Unexpected non-object JSON response from Osmund API",
                            status=response.status,
                        )
                    return payload

                self._raise_for_error(response.status, response.headers, payload)
                raise AssertionError("Unreachable")  # pragma: no cover
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OsmundConnectionError(
                f"Error communicating with Osmund API: {err}"
            ) from err

    @staticmethod
    def _raise_for_error(
        status: int,
        headers: Any,
        payload: Any,
    ) -> None:
        data = payload if isinstance(payload, dict) else {}
        code = data.get("code")
        message = data.get("message") or f"Osmund API error (HTTP {status})"
        request_id = data.get("requestId")
        retry_after = data.get("retryAfter")
        if retry_after is None:
            retry_after = _parse_int_header(headers, "Retry-After")
        else:
            try:
                retry_after = int(retry_after)
            except (TypeError, ValueError):
                retry_after = None

        if status == 401 or code == ERR_UNAUTHORIZED:
            raise OsmundAuthError(
                message, code=code, status=status, request_id=request_id
            )
        if code == ERR_SCOPE_REQUIRED:
            raise OsmundScopeError(
                message,
                scope=data.get("scope"),
                code=code,
                status=status,
                request_id=request_id,
            )
        if code == ERR_API_ACCESS_NOT_INCLUDED:
            raise OsmundAccessError(
                message,
                location_id=data.get("locationId"),
                code=code,
                status=status,
                request_id=request_id,
            )
        if status == 429 or code == ERR_RATE_LIMITED:
            raise OsmundRateLimitError(
                message,
                retry_after=retry_after,
                code=code,
                status=status,
                request_id=request_id,
            )
        if status == 503 or code == ERR_TEMPORARILY_UNAVAILABLE:
            raise OsmundConnectionError(
                message,
                retry_after=retry_after,
                code=code,
                status=status,
                request_id=request_id,
            )
        raise OsmundApiError(message, code=code, status=status, request_id=request_id)

    async def async_get_key_details(self) -> KeyDetails:
        """Fetch metadata about the current API key (GET /api/v1/me)."""
        data = await self._request("/api/v1/me")
        return KeyDetails.from_dict(data)

    async def async_list_locations(self) -> list[Location]:
        """Fetch all locations on the account (GET /api/v1/locations)."""
        data = await self._request("/api/v1/locations")
        return [Location.from_dict(item) for item in data.get("locations", [])]

    async def async_get_prices(
        self,
        location_id: str,
        *,
        date: str | None = None,
        resolution: str = "PT1H",
    ) -> dict[str, Any]:
        """Fetch electricity prices for a location."""
        params: dict[str, str | int] = {"resolution": resolution}
        if date is not None:
            params["date"] = date
        return await self._request(
            f"/api/v1/locations/{location_id}/prices", params=params
        )

    async def async_get_consumption(
        self,
        location_id: str,
        *,
        date_from: str,
        date_to: str,
        resolution: str = "P1D",
    ) -> dict[str, Any]:
        """Fetch kWh consumption for a location."""
        params: dict[str, str | int] = {
            "from": date_from,
            "to": date_to,
            "resolution": resolution,
        }
        return await self._request(
            f"/api/v1/locations/{location_id}/consumption", params=params
        )

    async def async_get_standby(self, location_id: str) -> dict[str, Any]:
        """Fetch monthly standby power for a location."""
        return await self._request(f"/api/v1/locations/{location_id}/standby")

    async def async_get_rated_cost(
        self,
        location_id: str,
        *,
        month: str | None = None,
    ) -> dict[str, Any]:
        """Fetch daily rated costs for a location."""
        params: dict[str, str | int] | None = (
            {"month": month} if month is not None else None
        )
        return await self._request(
            f"/api/v1/locations/{location_id}/costs/rated", params=params
        )

    async def async_list_invoices(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """Fetch a page of account invoices."""
        return await self._request(
            "/api/v1/invoices", params={"page": page, "perPage": per_page}
        )

    async def async_get_invoice(self, invoice_no: str) -> dict[str, Any]:
        """Fetch a single invoice with its line items."""
        return await self._request(f"/api/v1/invoices/{invoice_no}")
