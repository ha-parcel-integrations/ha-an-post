"""Diagnostics support for the An Post parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import AnPostConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address, a physical access code, or a specific
# parcel. Over-redacting is cheap; under-redacting leaks a user's home address
# — or a locker PIN — into a GitHub thread.
TO_REDACT = {
    # entry credentials
    CONF_EMAIL,
    CONF_PASSWORD,
    "email",
    "uid",
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # An Post payload fields — carrier-research/api/an-post/tracking.md
    "trackingNumber",
    "recipientName",
    "receiverName",
    "receiverNameTrunc",
    "recipientAddress",
    "destinationCountyCity",
    "destinationCountry",
    "deliveryPin",
    "parcelLockerPin",
    "orderNumber",
    "mrn",
    # the Gigya/JWT tokens themselves, should they ever end up in a dump
    "UID",
    "login_token",
    "id_token",
    "gmid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AnPostConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the An Post config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "account": async_redact_data(dict(entry.runtime_data.account), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
