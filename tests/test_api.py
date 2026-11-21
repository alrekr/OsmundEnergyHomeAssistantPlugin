"""Unit tests for the Osmund Energi API client."""

from __future__ import annotations

from datetime import date, datetime
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


def test_location_resolution_and_display_name(osmund_api: Any) -> None:
    """Verify auto/explicit price resolution and fallback display name."""
    loc_15m = osmund_api.Location.from_dict(
        {
            "id": "0192f3a1-7c2e-7b1a-9d3e-2f6a8c1e4b10",
            "label": "Hjem",
            "priceArea": "DK1",
            "state": "active",
            "plan": "osmund_rolig",
            "apiAccess": "open",
            "plus": {"status": "included", "trialEndsOn": None, "endsOn": None},
            "consumptionThrough": None,
            "meterResolution": "PT15M",
            "ratedThrough": None,
        }
    )
    assert loc_15m.display_name == "Hjem"
    assert loc_15m.resolved_price_resolution("auto") == "PT15M"
    assert loc_15m.resolved_price_resolution("hourly") == "PT1H"
    assert loc_15m.resolved_price_resolution("PT1H") == "PT1H"
    assert loc_15m.resolved_price_resolution("quarter_hourly") == "PT15M"

    loc_no_label = osmund_api.Location.from_dict(
        {
            "id": "0192f3a1-9a41-7d02-8c11-5b7e3d2f6a22",
            "label": None,
            "priceArea": "DK2",
            "state": "active",
            "plan": None,
            "apiAccess": "open",
            "plus": {"status": "active", "trialEndsOn": None, "endsOn": None},
            "consumptionThrough": None,
            "meterResolution": None,
            "ratedThrough": None,
        }
    )
    assert loc_no_label.display_name == "Location 0192f3a1"
    assert loc_no_label.resolved_price_resolution("auto") == "PT1H"


async def test_get_prices_and_interval_helpers(osmund_api: Any) -> None:
    """Parse PriceDay and verify current/next/min/max/avg helpers."""
    today_payload = {
        "locationId": "loc-1",
        "date": "2026-11-21",
        "resolution": "PT1H",
        "priceArea": "DK1",
        "currency": "DKK",
        "unit": "kr/kWh",
        "complete": True,
        "reason": None,
        "intervals": [
            {
                "start": "2026-11-21T22:00:00+01:00",
                "end": "2026-11-21T23:00:00+01:00",
                "spot": 0.40,
                "totalExVat": 0.80,
                "totalIncVat": 1.00,
                "components": [
                    {"type": "spot", "description": "Spotpris", "perKwh": 0.40},
                    {"type": "tariff", "description": "Nettarif", "perKwh": 0.25},
                    {"type": "tariff", "description": "Systemtarif", "perKwh": 0.07},
                    {"type": "tax", "description": "Elafgift", "perKwh": 0.08},
                ],
            },
            {
                "start": "2026-11-21T23:00:00+01:00",
                "end": "2026-11-22T00:00:00+01:00",
                "spot": 0.20,
                "totalExVat": 0.60,
                "totalIncVat": 0.75,
                "components": [
                    {"type": "spot", "description": "Spotpris", "perKwh": 0.20},
                    {"type": "tariff", "description": "Nettarif", "perKwh": 0.32},
                    {"type": "tax", "description": "Elafgift", "perKwh": 0.08},
                ],
            },
        ],
    }
    tomorrow_payload = {
        "locationId": "loc-1",
        "date": "2026-11-22",
        "resolution": "PT1H",
        "priceArea": "DK1",
        "currency": "DKK",
        "unit": "kr/kWh",
        "complete": True,
        "reason": None,
        "intervals": [
            {
                "start": "2026-11-22T00:00:00+01:00",
                "end": "2026-11-22T01:00:00+01:00",
                "spot": 0.15,
                "totalExVat": 0.50,
                "totalIncVat": 0.625,
                "components": [],
            }
        ],
    }

    client = osmund_api.OsmundApiClient(
        "osm_test", _MockSession(_MockResponse(200, today_payload))
    )
    today = await client.async_get_prices(
        "loc-1", date="2026-11-21", resolution="PT1H"
    )
    tomorrow = osmund_api.PriceDay.from_dict(tomorrow_payload)

    now_2230 = datetime.fromisoformat("2026-11-21T22:30:00+01:00")
    cur = today.interval_at(now_2230)
    assert cur is not None
    assert cur.total_inc_vat == 1.00
    assert cur.tariff_ex_vat == 0.32
    assert cur.tax_ex_vat == 0.08

    nxt = osmund_api.find_next_interval(today, tomorrow, now_2230)
    assert nxt is not None
    assert nxt.total_inc_vat == 0.75

    # Cross midnight from today's last interval into tomorrow's first interval
    now_2330 = datetime.fromisoformat("2026-11-21T23:30:00+01:00")
    nxt_midnight = osmund_api.find_next_interval(today, tomorrow, now_2330)
    assert nxt_midnight is not None
    assert nxt_midnight.start_iso == "2026-11-22T00:00:00+01:00"
    assert nxt_midnight.total_inc_vat == 0.625

    min_iv = today.min_interval()
    max_iv = today.max_interval()
    assert min_iv is not None
    assert min_iv.total_inc_vat == 0.75
    assert min_iv.start == datetime.fromisoformat("2026-11-21T23:00:00+01:00")
    assert max_iv is not None
    assert max_iv.total_inc_vat == 1.00
    assert today.avg_price_inc_vat() == 0.875

    svc = today.to_service_dict()
    assert svc["complete"] is True
    assert len(svc["intervals"]) == 2
    assert svc["intervals"][0]["total_inc_vat"] == 1.00


async def test_consumption_standby_rated_cost_and_invoices(osmund_api: Any) -> None:
    """Parse consumption, standby, rated cost, and invoice responses."""
    consumption_payload = {
        "locationId": "loc-1",
        "resolution": "P1D",
        "from": "2026-10-29",
        "to": "2026-11-02",
        "unit": "kWh",
        "totalKwh": 40.5,
        "values": [
            {
                "start": "2026-10-31T00:00:00+01:00",
                "end": "2026-11-01T00:00:00+01:00",
                "kwh": 12.0,
            },
            {
                "start": "2026-11-01T00:00:00+01:00",
                "end": "2026-11-02T00:00:00+01:00",
                "kwh": 13.5,
            },
            {
                "start": "2026-11-02T00:00:00+01:00",
                "end": "2026-11-03T00:00:00+01:00",
                "kwh": 15.0,
            },
        ],
    }
    client = osmund_api.OsmundApiClient(
        "osm_test", _MockSession(_MockResponse(200, consumption_payload))
    )
    cons = await client.async_get_consumption(
        "loc-1", date_from="2026-10-29", date_to="2026-11-02"
    )
    assert cons.latest_value is not None
    assert cons.latest_value.kwh == 15.0
    assert cons.latest_value.date_str == "2026-11-02"
    # Excludes October's 12.0 kWh when computing November total
    assert cons.total_for_month("2026-11") == 28.5
    assert cons.total_for_month("2026-12") is None

    standby = osmund_api.Standby.from_dict(
        {
            "locationId": "loc-1",
            "unit": "kW",
            "months": [
                {"month": "2026-09", "standbyKw": 0.142},
                {"month": "2026-10", "standbyKw": 0.151},
            ],
        }
    )
    assert standby.latest_month is not None
    assert standby.latest_month.month == "2026-10"
    assert standby.latest_month.standby_kw == 0.151

    rated = osmund_api.RatedCost.from_dict(
        {
            "locationId": "loc-1",
            "month": "2026-11",
            "currency": "DKK",
            "complete": False,
            "missingDays": ["2026-11-03"],
            "daysInMonth": 30,
            "daysWithData": 1,
            "totalKwh": 13.9,
            "total": {"exVat": 18.92, "incVat": 23.65},
            "subscriptions": {"exVat": 3.11, "incVat": 3.89},
            "totalWithSubscriptions": {"exVat": 22.03, "incVat": 27.54},
            "subscriptionsComplete": True,
            "days": [
                {
                    "date": "2026-11-01",
                    "kwh": 13.9,
                    "exVat": 18.92,
                    "incVat": 23.65,
                    "subscriptions": {"exVat": 3.11, "incVat": 3.89},
                    "totalWithSubscriptions": {"exVat": 22.03, "incVat": 27.54},
                    "components": [],
                }
            ],
        }
    )
    assert rated.total_with_subscriptions.inc_vat == 27.54
    assert rated.latest_day is not None
    assert rated.latest_day.date == "2026-11-01"

    invoices = osmund_api.InvoiceList.from_dict(
        {
            "page": 1,
            "perPage": 25,
            "hasMore": False,
            "invoices": [
                {
                    "invoiceNo": "F200",
                    "type": "invoice",
                    "paid": False,
                    "invoiceDate": "2026-11-12",
                    "dueDate": "2026-11-26",
                    "currency": "DKK",
                    "total": {"exVat": 400.0, "incVat": 500.0},
                    "outstanding": 500.0,
                },
                {
                    "invoiceNo": "F199",
                    "type": "invoice",
                    "paid": False,
                    "invoiceDate": "2026-10-12",
                    "dueDate": "2026-11-20",
                    "currency": "DKK",
                    "total": {"exVat": 200.0, "incVat": 250.0},
                    "outstanding": 125.5,
                },
                {
                    "invoiceNo": "F198",
                    "type": "invoice",
                    "paid": True,
                    "invoiceDate": "2026-09-12",
                    "dueDate": "2026-09-26",
                    "currency": "DKK",
                    "total": {"exVat": 100.0, "incVat": 125.0},
                    "outstanding": 0.0,
                },
            ],
        }
    )
    assert invoices.latest_invoice is not None
    assert invoices.latest_invoice.invoice_no == "F200"
    assert invoices.outstanding_balance == 625.5
    assert invoices.next_due_date == date(2026, 11, 20)
