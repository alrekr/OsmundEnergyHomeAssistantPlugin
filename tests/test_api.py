"""Unit tests for the Osmund Energi API client."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest


class _MockResponse:
    """Minimal async context manager emulating aiohttp.ClientResponse."""

    def __init__(
        self,
        status: int,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    async def json(self, *, content_type: str | None = None) -> Any:
        return self._payload

    async def __aenter__(self) -> _MockResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _MockSession:
    """Minimal fake aiohttp.ClientSession recording calls."""

    def __init__(self, response: _MockResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> _MockResponse:
        self.calls.append(
            {"url": url, "headers": headers, "params": params, "timeout": timeout}
        )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_repr_redacts_api_key(osmund_api: Any) -> None:
    """Ensure API key never appears in client repr."""
    fake_key = "osm_" + "secretvalue1234567890"
    client = osmund_api.OsmundApiClient(fake_key, _MockSession(_MockResponse(200, {})))
    assert fake_key not in repr(client)
    assert "***" in repr(client)


def test_empty_api_key_rejected(osmund_api: Any) -> None:
    """Reject empty or whitespace-only API keys."""
    with pytest.raises(ValueError):
        osmund_api.OsmundApiClient("   ", _MockSession(_MockResponse(200, {})))


async def test_get_key_details_and_rate_limits(osmund_api: Any) -> None:
    """Parse /api/v1/me response, unknown beta fields, and rate limit headers."""
    response = _MockResponse(
        200,
        {
            "key": {
                "name": "Home Assistant",
                "prefix": "osm_4f9Kx2Qa",
                "scopes": ["prices:read", "consumption:read"],
                "expiresAt": "2027-11-16T09:12:00+01:00",
                "futureBetaField": "ignored-safely",
            }
        },
        headers={
            "RateLimit-Limit": "60",
            "RateLimit-Remaining": "59",
            "RateLimit-Reset": "45",
        },
    )
    session = _MockSession(response)
    fake_key = "osm_" + "testkey1234567890"
    client = osmund_api.OsmundApiClient(fake_key, session)

    details = await client.async_get_key_details()

    assert details.name == "Home Assistant"
    assert details.prefix == "osm_4f9Kx2Qa"
    assert details.scopes == ("prices:read", "consumption:read")
    assert details.expires_at == "2027-11-16T09:12:00+01:00"
    assert client.last_rate_limit == osmund_api.RateLimitInfo(
        limit=60, remaining=59, reset=45
    )
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {fake_key}"


async def test_list_locations(osmund_api: Any) -> None:
    """Parse /api/v1/locations including open and closed locations."""
    response = _MockResponse(
        200,
        {
            "locations": [
                {
                    "id": "0192f3a1-7c2e-7b1a-9d3e-2f6a8c1e4b10",
                    "label": "Hjem",
                    "priceArea": "DK1",
                    "state": "active",
                    "plan": "osmund_rolig",
                    "apiAccess": "open",
                    "plus": {
                        "status": "included",
                        "trialEndsOn": None,
                        "endsOn": None,
                    },
                    "consumptionThrough": "2026-11-16T00:00:00+01:00",
                    "meterResolution": "PT15M",
                    "ratedThrough": "2026-11-15",
                },
                {
                    "id": "0192f3a1-9a41-7d02-8c11-5b7e3d2f6a22",
                    "label": None,
                    "priceArea": "DK2",
                    "state": "upcoming",
                    "plan": None,
                    "apiAccess": "closed",
                    "plus": {
                        "status": "none",
                        "trialEndsOn": None,
                        "endsOn": None,
                    },
                    "consumptionThrough": None,
                    "meterResolution": None,
                    "ratedThrough": None,
                },
            ]
        },
    )
    client = osmund_api.OsmundApiClient("osm_test", _MockSession(response))
    locations = await client.async_list_locations()

    assert len(locations) == 2
    assert locations[0].has_api_access is True
    assert locations[0].label == "Hjem"
    assert locations[0].plus.status == "included"
    assert locations[1].has_api_access is False
    assert locations[1].label is None


async def test_error_mapping(osmund_api: Any) -> None:
    """Verify structured API error codes map to typed exceptions."""
    cases = [
        (
            401,
            {"code": "unauthorized", "message": "Invalid key", "requestId": "req-1"},
            osmund_api.OsmundAuthError,
        ),
        (
            403,
            {
                "code": "scope_required",
                "scope": "prices:read",
                "message": "Missing scope",
                "requestId": "req-2",
            },
            osmund_api.OsmundScopeError,
        ),
        (
            403,
            {
                "code": "api_access_not_included",
                "locationId": "loc-1",
                "message": "No Plus",
                "requestId": "req-3",
            },
            osmund_api.OsmundAccessError,
        ),
        (
            429,
            {
                "code": "rate_limited",
                "retryAfter": 42,
                "message": "Slow down",
                "requestId": "req-4",
            },
            osmund_api.OsmundRateLimitError,
        ),
        (
            503,
            {
                "code": "temporarily_unavailable",
                "retryAfter": 30,
                "message": "Try later",
                "requestId": "req-5",
            },
            osmund_api.OsmundConnectionError,
        ),
    ]

    for status, payload, expected_exc in cases:
        client = osmund_api.OsmundApiClient(
            "osm_test", _MockSession(_MockResponse(status, payload))
        )
        with pytest.raises(expected_exc) as exc_info:
            await client.async_get_key_details()
        assert exc_info.value.code == payload["code"]
        assert exc_info.value.request_id == payload["requestId"]


async def test_network_error_raises_connection_error(osmund_api: Any) -> None:
    """Map aiohttp network failures to OsmundConnectionError."""
    client = osmund_api.OsmundApiClient(
        "osm_test", _MockSession(aiohttp.ClientError("boom"))
    )
    with pytest.raises(osmund_api.OsmundConnectionError):
        await client.async_get_key_details()
