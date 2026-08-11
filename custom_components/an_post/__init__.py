"""An Post parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AnPostApiClient,
    AnPostApiError,
    AnPostAuthError,
)
from .const import DOMAIN, PLATFORMS
from .coordinator import AnPostCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class AnPostData:
    """Runtime data attached to an An Post config entry."""

    client: AnPostApiClient
    coordinator: AnPostCoordinator
    account: dict[str, Any]
    session: aiohttp.ClientSession


type AnPostConfigEntry = ConfigEntry[AnPostData]


async def async_setup_entry(
    hass: HomeAssistant, entry: AnPostConfigEntry
) -> bool:
    """Set up An Post from a config entry."""
    # Each config entry needs its own cookie jar, or two accounts overwrite
    # each other's auth cookies in the shared session. The connector is reused
    # (connector_owner=False) so this stays cheap.
    session = aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
    )
    client = AnPostApiClient(
        entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], session
    )

    try:
        account = await client.async_login()
    except AnPostAuthError as err:
        # Credentials rejected — start reauth instead of retrying a password
        # that will never work again.
        await session.close()
        raise ConfigEntryAuthFailed("An Post authentication failed") from err
    except (AnPostApiError, aiohttp.ClientError) as err:
        # Non-auth failure (typically a 5xx outage) — retry with backoff.
        await session.close()
        raise ConfigEntryNotReady("An Post login failed") from err

    coordinator = AnPostCoordinator(hass, client, entry)

    try:
        # Fetch initial data here, before forwarding to platforms. Raising
        # ConfigEntryNotReady from a forwarded platform is too late for HA to
        # catch cleanly (it logs a warning and half-sets-up the entry); doing
        # the first refresh here lets a transient failure fail the whole entry
        # so HA retries it with backoff.
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Without this, every setup retry leaks a session.
        await session.close()
        raise

    entry.runtime_data = AnPostData(
        client=client, coordinator=coordinator, account=account, session=session
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await session.close()
        raise

    # Registers an_post.track_parcel / untrack_parcel (the account's
    # server-side watchlist), idempotently — multiple accounts share the same
    # two services and target one another via a config_entry_id field.
    async_setup_services(hass)

    # No entry.add_update_listener: the options flow calls
    # async_schedule_reload itself. Combining an update listener with a
    # reload-on-update flow is deprecated and becomes an error in HA 2026.12+.
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: AnPostConfigEntry
) -> bool:
    """Unload an An Post config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.session.close()
    # Multiple accounts may be configured; only drop the services once the
    # last one goes.
    remaining = [e for e in hass.config_entries.async_entries(DOMAIN) if e is not entry]
    if not remaining:
        async_unload_services(hass)
    return True
