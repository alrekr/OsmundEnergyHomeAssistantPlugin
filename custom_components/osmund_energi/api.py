"""Async client for the Osmund Energi API.

This module is intentionally Home Assistant agnostic so it can be tested
directly in scripts/unit tests and extracted into a standalone PyPI package if
this integration moves to Home Assistant Core.

OpenAPI specification (v1.1.0-beta):
https://osmundenergi.dk/docs/osmund-api/openapi.yaml
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import aiohttp

from .const import (
    API_BASE_URL,
    ERR_API_ACCESS_NOT_INCLUDED,
    ERR_RATE_LIMITED,
    ERR_SCOPE_REQUIRED,
    ERR_TEMPORARILY_UNAVAILABLE,
    ERR_UNAUTHORIZED,
    OPTION_TO_API_RESOLUTION,
    PRICE_RESOLUTION_HOURLY,
    PRICE_RESOLUTION_QUARTER_HOURLY,
)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string into an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_iso_date(value: str | None) -> date | None:
    """Parse an ISO 8601 YYYY-MM-DD date string into a date, or None."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
class Amount:
    """An amount in DKK without and with VAT."""

    ex_vat: float
    inc_vat: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Amount:
        return cls(
            ex_vat=float(data.get("exVat", 0.0)),
            inc_vat=float(data.get("incVat", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class KeyDetails:
    """Metadata about the active API key from GET /api/v1/me."""

    name: str
    prefix: str
    scopes: tuple[str, ...]
    expires_at: str

    def has_scope(self, scope: str) -> bool:
        """Return True if the API key includes the given scope."""
        return scope in self.scopes

    @property
    def expires_at_dt(self) -> datetime | None:
        """Return the expiry timestamp as an aware datetime."""
        return parse_iso_datetime(self.expires_at)

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

    @property
    def display_name(self) -> str:
        """Return the location label or a deterministic fallback name."""
        if self.label and self.label.strip():
            return self.label.strip()
        return f"Location {self.id[:8]}"

    def resolved_price_resolution(self, configured: str) -> str:
        """Resolve the effective API price resolution (`PT1H` or `PT15M`)."""
        if configured in OPTION_TO_API_RESOLUTION:
            return OPTION_TO_API_RESOLUTION[configured]
        if self.meter_resolution in (
            PRICE_RESOLUTION_HOURLY,
            PRICE_RESOLUTION_QUARTER_HOURLY,
        ):
            return self.meter_resolution
        return PRICE_RESOLUTION_HOURLY

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


@dataclass(frozen=True, slots=True)
class PriceComponent:
    """A single price component (spot, tariff, tax, supplier) in kr/kWh excl. VAT."""

    type: str
    description: str
    per_kwh: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "type": self.type,
            "description": self.description,
            "per_kwh": self.per_kwh,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriceComponent:
        return cls(
            type=str(data.get("type", "unknown")),
            description=str(data.get("description", "")),
            per_kwh=float(data.get("perKwh", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class PriceInterval:
    """Electricity price for a single hour or quarter hour."""

    start: datetime
    end: datetime
    start_iso: str
    end_iso: str
    spot: float
    total_ex_vat: float
    total_inc_vat: float
    components: tuple[PriceComponent, ...]

    @property
    def tariff_ex_vat(self) -> float:
        """Return the combined grid/system tariff in kr/kWh excl. VAT."""
        return round(
            sum(c.per_kwh for c in self.components if c.type == "tariff"),
            6,
        )

    @property
    def tax_ex_vat(self) -> float:
        """Return the combined tax components in kr/kWh excl. VAT."""
        return round(
            sum(c.per_kwh for c in self.components if c.type == "tax"),
            6,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation for service responses."""
        return {
            "start": self.start_iso,
            "end": self.end_iso,
            "spot": self.spot,
            "total_ex_vat": self.total_ex_vat,
            "total_inc_vat": self.total_inc_vat,
            "components": [c.to_dict() for c in self.components],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriceInterval:
        start_iso = str(data["start"])
        end_iso = str(data["end"])
        return cls(
            start=datetime.fromisoformat(start_iso),
            end=datetime.fromisoformat(end_iso),
            start_iso=start_iso,
            end_iso=end_iso,
            spot=float(data["spot"]),
            total_ex_vat=float(data["totalExVat"]),
            total_inc_vat=float(data["totalIncVat"]),
            components=tuple(
                PriceComponent.from_dict(c) for c in data.get("components", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class PriceDay:
    """Electricity prices for a single Danish calendar day."""

    location_id: str
    date: str
    resolution: str
    price_area: str | None
    currency: str
    unit: str
    complete: bool
    reason: str | None
    intervals: tuple[PriceInterval, ...]

    def interval_at(self, now: datetime) -> PriceInterval | None:
        """Return the interval containing `now`, or None."""
        for interval in self.intervals:
            if interval.start <= now < interval.end:
                return interval
        return None

    def min_interval(self) -> PriceInterval | None:
        """Return the earliest interval with the minimum total_inc_vat."""
        if not self.intervals:
            return None
        return min(self.intervals, key=lambda i: i.total_inc_vat)

    def max_interval(self) -> PriceInterval | None:
        """Return the earliest interval with the maximum total_inc_vat."""
        if not self.intervals:
            return None
        return max(self.intervals, key=lambda i: i.total_inc_vat)

    def avg_price_inc_vat(self) -> float | None:
        """Return the arithmetic mean of total_inc_vat across intervals."""
        if not self.intervals:
            return None
        return round(
            sum(i.total_inc_vat for i in self.intervals) / len(self.intervals),
            5,
        )

    def to_service_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable day summary for service responses."""
        return {
            "complete": self.complete,
            "reason": self.reason,
            "intervals": [interval.to_dict() for interval in self.intervals],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriceDay:
        return cls(
            location_id=str(data["locationId"]),
            date=str(data["date"]),
            resolution=str(data["resolution"]),
            price_area=data.get("priceArea"),
            currency=str(data.get("currency", "DKK")),
            unit=str(data.get("unit", "kr/kWh")),
            complete=bool(data.get("complete", False)),
            reason=data.get("reason"),
            intervals=tuple(
                PriceInterval.from_dict(item) for item in data.get("intervals", ())
            ),
        )


def find_next_interval(
    today: PriceDay | None,
    tomorrow: PriceDay | None,
    now: datetime,
) -> PriceInterval | None:
    """Return the first price interval starting strictly after `now`."""
    candidates: list[PriceInterval] = []
    if today is not None:
        candidates.extend(today.intervals)
    if tomorrow is not None:
        candidates.extend(tomorrow.intervals)
    for interval in candidates:
        if interval.start > now:
            return interval
    return None


@dataclass(frozen=True, slots=True)
class ConsumptionValue:
    """A single kWh reading period."""

    start: datetime
    end: datetime
    start_iso: str
    end_iso: str
    kwh: float

    @property
    def date_str(self) -> str:
        """Return the YYYY-MM-DD start date in the reading's local offset."""
        return self.start_iso[:10]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsumptionValue:
        start_iso = str(data["start"])
        end_iso = str(data["end"])
        return cls(
            start=datetime.fromisoformat(start_iso),
            end=datetime.fromisoformat(end_iso),
            start_iso=start_iso,
            end_iso=end_iso,
            kwh=float(data["kwh"]),
        )


@dataclass(frozen=True, slots=True)
class Consumption:
    """Consumption response from GET /api/v1/locations/{id}/consumption."""

    location_id: str
    resolution: str
    date_from: str
    date_to: str
    unit: str
    total_kwh: float
    values: tuple[ConsumptionValue, ...]

    @property
    def latest_value(self) -> ConsumptionValue | None:
        """Return the most recent reading period, if any."""
        return self.values[-1] if self.values else None

    def total_for_month(self, month_prefix: str) -> float | None:
        """Return total kWh for values starting in `YYYY-MM`, or None if none."""
        matching = [v.kwh for v in self.values if v.start_iso.startswith(month_prefix)]
        if not matching:
            return None
        return round(sum(matching), 4)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Consumption:
        return cls(
            location_id=str(data["locationId"]),
            resolution=str(data["resolution"]),
            date_from=str(data["from"]),
            date_to=str(data["to"]),
            unit=str(data.get("unit", "kWh")),
            total_kwh=float(data.get("totalKwh", 0.0)),
            values=tuple(
                ConsumptionValue.from_dict(v) for v in data.get("values", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class StandbyMonth:
    """Standby power (10th percentile hourly load) for a completed month."""

    month: str
    standby_kw: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StandbyMonth:
        return cls(
            month=str(data["month"]),
            standby_kw=float(data["standbyKw"]),
        )


@dataclass(frozen=True, slots=True)
class Standby:
    """Standby power response from GET /api/v1/locations/{id}/standby."""

    location_id: str
    unit: str
    months: tuple[StandbyMonth, ...]

    @property
    def latest_month(self) -> StandbyMonth | None:
        """Return the most recent completed standby month, if any."""
        if not self.months:
            return None
        return max(self.months, key=lambda m: m.month)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Standby:
        return cls(
            location_id=str(data["locationId"]),
            unit=str(data.get("unit", "kW")),
            months=tuple(StandbyMonth.from_dict(m) for m in data.get("months", ())),
        )


@dataclass(frozen=True, slots=True)
class CostComponent:
    """A single daily cost line item."""

    type: str
    description: str
    ex_vat: float
    inc_vat: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CostComponent:
        return cls(
            type=str(data.get("type", "unknown")),
            description=str(data.get("description", "")),
            ex_vat=float(data.get("exVat", 0.0)),
            inc_vat=float(data.get("incVat", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class RatedCostDay:
    """Rated cost for a single Danish calendar day."""

    date: str
    kwh: float
    ex_vat: float
    inc_vat: float
    subscriptions: Amount
    total_with_subscriptions: Amount
    components: tuple[CostComponent, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RatedCostDay:
        return cls(
            date=str(data["date"]),
            kwh=float(data.get("kwh", 0.0)),
            ex_vat=float(data.get("exVat", 0.0)),
            inc_vat=float(data.get("incVat", 0.0)),
            subscriptions=Amount.from_dict(data.get("subscriptions") or {}),
            total_with_subscriptions=Amount.from_dict(
                data.get("totalWithSubscriptions") or {}
            ),
            components=tuple(
                CostComponent.from_dict(c) for c in data.get("components", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class RatedCost:
    """Monthly rated cost response from GET /api/v1/locations/{id}/costs/rated."""

    location_id: str
    month: str
    currency: str
    complete: bool
    missing_days: tuple[str, ...]
    days_in_month: int
    days_with_data: int
    total_kwh: float
    total: Amount
    subscriptions: Amount
    total_with_subscriptions: Amount
    subscriptions_complete: bool
    days: tuple[RatedCostDay, ...]

    @property
    def latest_day(self) -> RatedCostDay | None:
        """Return the most recent rated day in this month, if any."""
        return self.days[-1] if self.days else None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RatedCost:
        return cls(
            location_id=str(data["locationId"]),
            month=str(data["month"]),
            currency=str(data.get("currency", "DKK")),
            complete=bool(data.get("complete", False)),
            missing_days=tuple(str(d) for d in data.get("missingDays", ())),
            days_in_month=int(data.get("daysInMonth", 0)),
            days_with_data=int(data.get("daysWithData", 0)),
            total_kwh=float(data.get("totalKwh", 0.0)),
            total=Amount.from_dict(data.get("total") or {}),
            subscriptions=Amount.from_dict(data.get("subscriptions") or {}),
            total_with_subscriptions=Amount.from_dict(
                data.get("totalWithSubscriptions") or {}
            ),
            subscriptions_complete=bool(data.get("subscriptionsComplete", False)),
            days=tuple(RatedCostDay.from_dict(d) for d in data.get("days", ())),
        )


@dataclass(frozen=True, slots=True)
class InvoiceSummary:
    """Summary of an account invoice from GET /api/v1/invoices."""

    invoice_no: str
    type: str
    paid: bool
    invoice_date: str | None
    due_date: str | None
    currency: str
    total: Amount
    outstanding: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvoiceSummary:
        return cls(
            invoice_no=str(data["invoiceNo"]),
            type=str(data.get("type", "invoice")),
            paid=bool(data.get("paid", False)),
            invoice_date=data.get("invoiceDate"),
            due_date=data.get("dueDate"),
            currency=str(data.get("currency", "DKK")),
            total=Amount.from_dict(data.get("total") or {}),
            outstanding=float(data.get("outstanding", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class InvoiceList:
    """Paginated invoice list from GET /api/v1/invoices (newest first)."""

    invoices: tuple[InvoiceSummary, ...]
    page: int
    per_page: int
    has_more: bool

    @property
    def latest_invoice(self) -> InvoiceSummary | None:
        """Return the newest invoice, if any."""
        return self.invoices[0] if self.invoices else None

    @property
    def outstanding_balance(self) -> float:
        """Return total unpaid balance in DKK incl. VAT."""
        return round(
            sum(inv.outstanding for inv in self.invoices if not inv.paid),
            2,
        )

    @property
    def next_due_date(self) -> date | None:
        """Return the earliest due date among unpaid invoices, or None."""
        due_dates = [
            parsed
            for inv in self.invoices
            if not inv.paid and (parsed := parse_iso_date(inv.due_date)) is not None
        ]
        return min(due_dates) if due_dates else None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvoiceList:
        return cls(
            invoices=tuple(
                InvoiceSummary.from_dict(inv) for inv in data.get("invoices", ())
            ),
            page=int(data.get("page", 1)),
            per_page=int(data.get("perPage", 25)),
            has_more=bool(data.get("hasMore", False)),
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
        resolution: str = PRICE_RESOLUTION_HOURLY,
    ) -> PriceDay:
        """Fetch electricity prices for a location."""
        params: dict[str, str | int] = {"resolution": resolution}
        if date is not None:
            params["date"] = date
        data = await self._request(
            f"/api/v1/locations/{location_id}/prices", params=params
        )
        return PriceDay.from_dict(data)

    async def async_get_consumption(
        self,
        location_id: str,
        *,
        date_from: str,
        date_to: str,
        resolution: str = "P1D",
    ) -> Consumption:
        """Fetch kWh consumption for a location."""
        params: dict[str, str | int] = {
            "from": date_from,
            "to": date_to,
            "resolution": resolution,
        }
        data = await self._request(
            f"/api/v1/locations/{location_id}/consumption", params=params
        )
        return Consumption.from_dict(data)

    async def async_get_standby(self, location_id: str) -> Standby:
        """Fetch monthly standby power for a location."""
        data = await self._request(f"/api/v1/locations/{location_id}/standby")
        return Standby.from_dict(data)

    async def async_get_rated_cost(
        self,
        location_id: str,
        *,
        month: str | None = None,
    ) -> RatedCost:
        """Fetch daily rated costs for a location."""
        params: dict[str, str | int] | None = (
            {"month": month} if month is not None else None
        )
        data = await self._request(
            f"/api/v1/locations/{location_id}/costs/rated", params=params
        )
        return RatedCost.from_dict(data)

    async def async_list_invoices(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
    ) -> InvoiceList:
        """Fetch a page of account invoices."""
        data = await self._request(
            "/api/v1/invoices", params={"page": page, "perPage": per_page}
        )
        return InvoiceList.from_dict(data)

    async def async_get_invoice(self, invoice_no: str) -> dict[str, Any]:
        """Fetch a single invoice with its line items."""
        return await self._request(f"/api/v1/invoices/{invoice_no}")
