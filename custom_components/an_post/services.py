"""Services for the An Post parcel tracker integration.

Unlike the account-less carriers in this suite, An Post's ``track_parcel`` /
``untrack_parcel`` do not touch local config-entry storage — a watchlisted
number is An Post's **own** server-side mechanism (``POST``/``DELETE
.../watchlist/{trackingNumber}``, see ``carrier-research/api/an-post/
tracking.md``), and a watchlisted item is folded into the account's regular
``trackingitems`` inbox automatically. So these services just call the live
API; the coordinator's next poll picks the parcel up with no further plumbing.

An Post allows more than one configured account (unlike the single-hub
account-less carriers), so both services take a ``config_entry_id`` selecting
which account's watchlist to change.
"""
from __future__ import annotations

import re

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .api import AnPostApiClient, AnPostApiError, AnPostAuthError
from .const import DOMAIN, TRACKING_CODE_PATTERN

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

CONF_CONFIG_ENTRY_ID = "config_entry_id"
CONF_TRACKING_CODE = "tracking_code"

_TRACKING_CODE_RE = re.compile(TRACKING_CODE_PATTERN)

_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): selector.ConfigEntrySelector(
            selector.ConfigEntrySelectorConfig(integration=DOMAIN)
        ),
        vol.Required(CONF_TRACKING_CODE): cv.string,
    }
)


def normalize_tracking_code(tracking_code: str) -> str:
    """Normalise a user-entered An Post tracking code for comparison/lookup."""
    return (tracking_code or "").strip().upper()


def valid_tracking_code(tracking_code: str) -> bool:
    """Return whether ``tracking_code`` matches An Post's S10 format."""
    return bool(_TRACKING_CODE_RE.fullmatch(tracking_code))


def _resolve_client(hass: HomeAssistant, config_entry_id: str) -> AnPostApiClient:
    """Return the loaded An Post account's API client, or raise."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        raise ServiceValidationError("That An Post account is not set up")
    return entry.runtime_data.client


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the An Post services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not valid_tracking_code(tracking_code):
            raise ServiceValidationError(
                f"'{tracking_code}' is not a valid An Post tracking number"
            )
        client = _resolve_client(hass, call.data[CONF_CONFIG_ENTRY_ID])
        try:
            await client.async_add_to_watchlist(tracking_code)
        except AnPostAuthError as err:
            raise ServiceValidationError(
                "An Post authentication failed — reauthenticate the account"
            ) from err
        except AnPostApiError as err:
            raise ServiceValidationError(
                f"An Post rejected the request: {err}"
            ) from err

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        client = _resolve_client(hass, call.data[CONF_CONFIG_ENTRY_ID])
        try:
            await client.async_remove_from_watchlist(tracking_code)
        except AnPostAuthError as err:
            raise ServiceValidationError(
                "An Post authentication failed — reauthenticate the account"
            ) from err
        except AnPostApiError as err:
            raise ServiceValidationError(
                f"An Post rejected the request: {err}"
            ) from err

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_SERVICE_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the An Post services (called once the last account unloads)."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
