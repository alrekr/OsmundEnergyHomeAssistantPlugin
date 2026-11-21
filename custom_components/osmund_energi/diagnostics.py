"""Diagnostics support for Osmund Energi."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import OsmundConfigEntry
from .const import CONF_REDACT_LOCATION_LABELS, DEFAULT_REDACT_LOCATION_LABELS

BASE_TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: OsmundConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for an Osmund Energi config entry."""
    runtime = entry.runtime_data
    account_data = runtime.account_coordinator.data
    price_data = runtime.price_coordinator.data or {}

    redact_labels = bool(
        entry.options.get(
            CONF_REDACT_LOCATION_LABELS, DEFAULT_REDACT_LOCATION_LABELS
        )
    )
    to_redact = set(BASE_TO_REDACT)
    if redact_labels:
        to_redact.add("label")

    rate_limit = runtime.client.last_rate_limit
    rate_limit_dict = asdict(rate_limit) if rate_limit is not None else None

    locations_summary: list[dict[str, Any]] = []
    if account_data is not None:
        for loc in account_data.locations.values():
            locations_summary.append(
                {
                    "id": loc.id,
                    "label": loc.label,
                    "price_area": loc.price_area,
                    "state": loc.state,
                    "plan": loc.plan,
                    "api_access": loc.api_access,
                    "plus": asdict(loc.plus),
                    "meter_resolution": loc.meter_resolution,
                    "consumption_through": loc.consumption_through,
                    "rated_through": loc.rated_through,
                }
            )

    prices_summary: dict[str, Any] = {}
    for loc_id, loc_prices in price_data.items():
        prices_summary[loc_id] = {
            "resolution": loc_prices.resolution,
            "today": (
                {
                    "date": loc_prices.today.date,
                    "complete": loc_prices.today.complete,
                    "reason": loc_prices.today.reason,
                    "interval_count": len(loc_prices.today.intervals),
                }
                if loc_prices.today is not None
                else None
            ),
            "tomorrow": (
                {
                    "date": loc_prices.tomorrow.date,
                    "complete": loc_prices.tomorrow.complete,
                    "reason": loc_prices.tomorrow.reason,
                    "interval_count": len(loc_prices.tomorrow.intervals),
                }
                if loc_prices.tomorrow is not None
                else None
            ),
        }

    metered_summary: dict[str, Any] = {}
    invoices_summary: dict[str, Any] | None = None
    if account_data is not None:
        for loc in account_data.open_locations:
            consumption = account_data.consumption.get(loc.id)
            standby = account_data.standby.get(loc.id)
            rated = account_data.rated_cost.get(loc.id)
            metered_summary[loc.id] = {
                "consumption": (
                    {
                        "from": consumption.date_from,
                        "to": consumption.date_to,
                        "days_returned": len(consumption.values),
                        "total_kwh": consumption.total_kwh,
                    }
                    if consumption is not None
                    else None
                ),
                "standby": (
                    {
                        "months_returned": len(standby.months),
                        "latest_month": (
                            asdict(standby.latest_month)
                            if standby.latest_month is not None
                            else None
                        ),
                    }
                    if standby is not None
                    else None
                ),
                "rated_cost": (
                    {
                        "month": rated.current_month.month,
                        "complete": rated.current_month.complete,
                        "days_with_data": rated.current_month.days_with_data,
                        "missing_days": list(rated.current_month.missing_days),
                        "latest_day": (
                            rated.latest_day.date
                            if rated.latest_day is not None
                            else None
                        ),
                    }
                    if rated is not None
                    else None
                ),
            }

        if account_data.invoices is not None:
            latest = account_data.invoices.latest_invoice
            invoices_summary = {
                "count": len(account_data.invoices.invoices),
                "has_more": account_data.invoices.has_more,
                "outstanding_balance": account_data.invoices.outstanding_balance,
                "latest_invoice_date": latest.invoice_date if latest else None,
            }

    payload: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "key_details": {
            "name": runtime.key_details.name,
            "prefix": runtime.key_details.prefix,
            "scopes": list(runtime.key_details.scopes),
            "expires_at": runtime.key_details.expires_at,
        },
        "rate_limit": rate_limit_dict,
        "coordinators": {
            "account_last_update_success": (
                runtime.account_coordinator.last_update_success
            ),
            "price_last_update_success": runtime.price_coordinator.last_update_success,
            "cooldown_until": (
                runtime.rate_limit_gate.cooldown_until.isoformat()
                if runtime.rate_limit_gate.cooldown_until is not None
                else None
            ),
        },
        "locations": locations_summary,
        "prices": prices_summary,
        "metered_data": metered_summary,
        "invoices": invoices_summary,
    }

    return async_redact_data(payload, to_redact)
