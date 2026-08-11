"""Tests for the An Post track_parcel / untrack_parcel services.

Unlike the account-less carriers, these call the account's *live* watchlist
endpoint (not local config-entry storage) and target one of possibly several
configured accounts via ``config_entry_id``.
"""
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.an_post.api import AnPostApiError, AnPostAuthError
from custom_components.an_post.const import DOMAIN
from custom_components.an_post.services import (
    SERVICE_TRACK_PARCEL,
    SERVICE_UNTRACK_PARCEL,
    async_setup_services,
    async_unload_services,
    normalize_tracking_code,
    valid_tracking_code,
)

VALID_CODE = "AB123456789IE"


def _loaded_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="user@example.test")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = type("RuntimeData", (), {"client": AsyncMock()})()
    return entry


# ---------------------------------------------------------------------------
# tracking-code validation
# ---------------------------------------------------------------------------


def test_normalize_tracking_code_strips_and_upper_cases():
    assert normalize_tracking_code(" ab123456789ie ") == "AB123456789IE"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("AB123456789IE", True),
        ("ab123456789ie", False),  # not normalized here — caller normalizes first
        ("AB123456789", False),  # missing country suffix
        ("A1234567890IE", False),  # wrong prefix shape
        ("", False),
    ],
)
def test_valid_tracking_code(code, expected):
    assert valid_tracking_code(code) is expected


# ---------------------------------------------------------------------------
# track_parcel / untrack_parcel
# ---------------------------------------------------------------------------


async def test_track_parcel_calls_watchlist_add(hass):
    entry = _loaded_entry(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_TRACK_PARCEL,
        {"config_entry_id": entry.entry_id, "tracking_code": " ab123456789ie "},
        blocking=True,
    )

    entry.runtime_data.client.async_add_to_watchlist.assert_awaited_once_with(
        VALID_CODE
    )


async def test_untrack_parcel_calls_watchlist_remove(hass):
    entry = _loaded_entry(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UNTRACK_PARCEL,
        {"config_entry_id": entry.entry_id, "tracking_code": VALID_CODE},
        blocking=True,
    )

    entry.runtime_data.client.async_remove_from_watchlist.assert_awaited_once_with(
        VALID_CODE
    )


async def test_track_parcel_rejects_a_malformed_code(hass):
    entry = _loaded_entry(hass)
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TRACK_PARCEL,
            {"config_entry_id": entry.entry_id, "tracking_code": "not-a-code"},
            blocking=True,
        )
    entry.runtime_data.client.async_add_to_watchlist.assert_not_awaited()


async def test_track_parcel_rejects_an_unknown_config_entry(hass):
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TRACK_PARCEL,
            {"config_entry_id": "does-not-exist", "tracking_code": VALID_CODE},
            blocking=True,
        )


async def test_track_parcel_surfaces_auth_errors(hass):
    entry = _loaded_entry(hass)
    entry.runtime_data.client.async_add_to_watchlist.side_effect = AnPostAuthError(
        "HTTP 401"
    )
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TRACK_PARCEL,
            {"config_entry_id": entry.entry_id, "tracking_code": VALID_CODE},
            blocking=True,
        )


async def test_track_parcel_surfaces_api_errors(hass):
    entry = _loaded_entry(hass)
    entry.runtime_data.client.async_add_to_watchlist.side_effect = AnPostApiError(
        "HTTP 500"
    )
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TRACK_PARCEL,
            {"config_entry_id": entry.entry_id, "tracking_code": VALID_CODE},
            blocking=True,
        )


async def test_untrack_parcel_surfaces_auth_errors(hass):
    entry = _loaded_entry(hass)
    entry.runtime_data.client.async_remove_from_watchlist.side_effect = (
        AnPostAuthError("HTTP 401")
    )
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_UNTRACK_PARCEL,
            {"config_entry_id": entry.entry_id, "tracking_code": VALID_CODE},
            blocking=True,
        )


async def test_untrack_parcel_surfaces_api_errors(hass):
    entry = _loaded_entry(hass)
    entry.runtime_data.client.async_remove_from_watchlist.side_effect = (
        AnPostApiError("HTTP 500")
    )
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_UNTRACK_PARCEL,
            {"config_entry_id": entry.entry_id, "tracking_code": VALID_CODE},
            blocking=True,
        )


def test_setup_services_is_idempotent(hass):
    async_setup_services(hass)
    async_setup_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)


def test_unload_services_removes_both(hass):
    async_setup_services(hass)
    async_unload_services(hass)
    assert not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)
    assert not hass.services.has_service(DOMAIN, SERVICE_UNTRACK_PARCEL)


def test_unload_services_is_a_noop_when_never_set_up(hass):
    async_unload_services(hass)  # must not raise
    assert not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)
