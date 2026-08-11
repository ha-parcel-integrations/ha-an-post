"""Tests for An Post setup and unload."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.an_post.api import (
    AnPostApiError,
    AnPostAuthError,
)
from custom_components.an_post.const import DOMAIN

from .payloads import ACTIVE_CODE, active_sample

EMAIL = "user@example.test"
CLIENT = "custom_components.an_post.api.AnPostApiClient"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "hunter2"},
    )


async def test_setup_and_unload(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(f"{CLIENT}.async_login", new=AsyncMock(return_value={})),
        patch(
            f"{CLIENT}.async_get_parcels",
            new=AsyncMock(return_value=[active_sample()]),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    incoming = hass.states.get("sensor.an_post_user_example_test_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_rejected_credentials_start_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        f"{CLIENT}.async_login",
        new=AsyncMock(side_effect=AnPostAuthError("HTTP 401")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


@pytest.mark.parametrize(
    "error",
    [AnPostApiError("HTTP 500"), aiohttp.ClientError("boom")],
)
async def test_outage_retries_instead_of_reauth(hass, error):
    """A 5xx must retry with backoff — never push the user into reauth."""
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(f"{CLIENT}.async_login", new=AsyncMock(side_effect=error)):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_setup_retries_when_first_refresh_fails(hass):
    """The first refresh runs in __init__.py, before platforms are forwarded.

    A failure there fails the whole entry (SETUP_RETRY) instead of — too late —
    half-setting-up the entry from a forwarded platform.
    """
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(f"{CLIENT}.async_login", new=AsyncMock(return_value={})),
        patch(
            f"{CLIENT}.async_get_parcels",
            new=AsyncMock(side_effect=AnPostApiError("HTTP 503")),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_failed_platform_setup_closes_the_session(hass):
    """Every failed-setup path must close the per-entry session, or each retry
    leaks one."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(f"{CLIENT}.async_login", new=AsyncMock(return_value={})),
        patch(
            f"{CLIENT}.async_get_parcels",
            new=AsyncMock(return_value=[active_sample()]),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(side_effect=RuntimeError("platform blew up")),
        ),
        patch("aiohttp.ClientSession.close", new=AsyncMock()) as close,
    ):
        # Home Assistant catches the platform error itself and marks the entry
        # failed; what matters here is that the session went with it.
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    close.assert_awaited()


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    parcels = AsyncMock(return_value=[active_sample()])
    with (
        patch(f"{CLIENT}.async_login", new=AsyncMock(return_value={})),
        patch(f"{CLIENT}.async_get_parcels", new=parcels),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
        )

        # The next poll returns a different parcel: the summary sensor spawns a
        # new per-parcel sensor and removes the stale one via the registry.
        parcels.return_value = [active_sample("EXAMPLE222222")]
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_EXAMPLE222222"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
            )
            is None
        )
