"""Services for the An Post parcel tracker integration.

Unlike the account-less carriers in this suite, An Post's ``track_parcel`` /
``untrack_parcel`` do not touch local config-entry storage — a watchlisted
number is An Post's **own** server-side mechanism (``POST``/``DELETE
.../watchlist/{trackingNumber}``), and a watchlisted item is folded into the
account's regular ``trackingitems`` inbox automatically. So these services just call the live
API; the coordinator's next poll picks the parcel up with no further plumbing.

An Post allows more than one configured account (unlike the single-hub
account-less carriers), so both services take a ``config_entry_id`` selecting
which account's watchlist to change. ``an_post.update_contents`` sets or clears
the text describing what is in a parcel, on the account(s) that list the code.
"""

from __future__ import annotations

import re

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, selector
import voluptuous as vol

from .api import AnPostApiError, AnPostAuthError
from .const import CONF_CONFIG_ENTRY_ID, CONF_CONTENTS, DOMAIN, TRACKING_CODE_PATTERN
from .contents import normalize_code

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
SERVICE_UPDATE_CONTENTS = "update_contents"

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
_TRACK_SCHEMA = _SERVICE_SCHEMA.extend({vol.Optional(CONF_CONTENTS): cv.string})
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)


def normalize_tracking_code(tracking_code: str) -> str:
    """Normalise a user-entered An Post tracking code for comparison/lookup."""
    return (tracking_code or "").strip().upper()


def valid_tracking_code(tracking_code: str) -> bool:
    """Return whether ``tracking_code`` matches An Post's S10 format."""
    return bool(_TRACKING_CODE_RE.fullmatch(tracking_code))


def _resolve_entry(hass: HomeAssistant, config_entry_id: str):
    """Return the loaded An Post account entry, or raise."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        raise ServiceValidationError("That An Post account is not set up")
    return entry


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
        entry = _resolve_entry(hass, call.data[CONF_CONFIG_ENTRY_ID])
        client = entry.runtime_data.client
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

        if CONF_CONTENTS not in call.data:
            return
        coordinator = entry.runtime_data.coordinator
        if coordinator.contents_store.async_set(
            tracking_code, call.data[CONF_CONTENTS]
        ):
            # A code the account does not list yet gets the value from the poll
            # that first sees it.
            coordinator.async_reapply_contents()

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass, call.data[CONF_CONFIG_ENTRY_ID])
        entry.runtime_data.coordinator.contents_store.async_set(tracking_code, None)
        client = entry.runtime_data.client
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

    async def _update_contents(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A tracking code is required")
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("An Post is not set up")
        if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(
                    f"'{entry_id}' is not a loaded An Post entry"
                )
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two accounts, so every account that lists it
            # gets the value; guessing an account would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if code in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(code, call.data.get(CONF_CONTENTS)):
                coordinator.async_reapply_contents()

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_CONTENTS,
        _update_contents,
        schema=_UPDATE_CONTENTS_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the An Post services (called once the last account unloads)."""
    for service in (
        SERVICE_TRACK_PARCEL,
        SERVICE_UNTRACK_PARCEL,
        SERVICE_UPDATE_CONTENTS,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
