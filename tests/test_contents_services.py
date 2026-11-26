"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.an_post.api import AnPostApiError
from custom_components.an_post.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
)
from custom_components.an_post.contents import CONTENTS_STORE_MAX_CODES, ContentsStore
from custom_components.an_post.coordinator import AnPostCoordinator

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_sample, delivered_sample

CLIENT = "custom_components.an_post.api.AnPostApiClient"
UNLISTED = "AB555555555IE"
STORE_KEY = f"{DOMAIN}.contents.%s"
OPTIONS = {CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 100}


def _entry(email: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=email,
        unique_id=email,
        data={"email": email, "password": "hunter2"},
        options=OPTIONS,
    )


async def _setup(hass, raws=None, email="user@example.test") -> MockConfigEntry:
    entry = _entry(email)
    entry.add_to_hass(hass)
    raws = [active_sample(), delivered_sample()] if raws is None else raws
    with (
        patch(f"{CLIENT}.async_login", new=AsyncMock(return_value={})),
        patch(f"{CLIENT}.async_get_parcels", new=AsyncMock(return_value=raws)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service: str, data: dict) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


def _by_code(entry: MockConfigEntry) -> dict[str, dict]:
    coordinator = entry.runtime_data.coordinator
    return {p["barcode"]: p for p in [*coordinator.data, *coordinator.delivered]}


def _contents(entry, code=ACTIVE_CODE):
    return _by_code(entry)[code]["contents"]


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass)
    assert _contents(entry) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass)

    await _call(
        hass,
        "update_contents",
        {"tracking_code": ACTIVE_CODE, "contents": " Running  shoes "},
    )
    assert _contents(entry) == "Running shoes"

    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )
    assert _contents(entry) == "Socks"

    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "  "}
    )
    assert _contents(entry) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass)
    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )
    await _call(hass, "update_contents", {"tracking_code": ACTIVE_CODE})
    assert _contents(entry) is None


async def test_update_contents_trims_and_matches_the_code(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "update_contents",
        {"tracking_code": f" {ACTIVE_CODE.lower()} ", "contents": "Socks"},
    )
    assert _contents(entry) == "Socks"


async def test_update_contents_works_for_a_code_that_is_not_listed(hass):
    entry = await _setup(hass)
    await _call(
        hass, "update_contents", {"tracking_code": UNLISTED, "contents": "Socks"}
    )
    assert entry.runtime_data.coordinator.contents_store.get(UNLISTED) == "Socks"


async def test_update_contents_covers_delivered_parcels(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "update_contents",
        {"tracking_code": DELIVERED_CODE, "contents": "Socks"},
    )
    assert _contents(entry, DELIVERED_CODE) == "Socks"


async def test_update_contents_never_polls_and_notifies_once_per_change(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    seen = []
    coordinator.async_add_listener(lambda: seen.append(1))
    with (
        patch(f"{CLIENT}.async_get_parcels", new=AsyncMock()) as poll,
        patch.object(AnPostCoordinator, "async_request_refresh") as refresh,
    ):
        data = {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
        await _call(hass, "update_contents", data)
        assert len(seen) == 1
        # An unchanged value writes nothing and notifies nobody.
        await _call(hass, "update_contents", data)
        assert len(seen) == 1
        await _call(hass, "update_contents", {"tracking_code": ACTIVE_CODE})
        assert len(seen) == 2
        poll.assert_not_called()
        refresh.assert_not_called()


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {"tracking_code": "  "})


async def test_unloading_the_last_account_drops_update_contents(hass):
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_track_parcel_stores_contents(hass):
    entry = await _setup(hass)
    with patch(f"{CLIENT}.async_add_to_watchlist", new=AsyncMock()) as add:
        await _call(
            hass,
            "track_parcel",
            {
                "config_entry_id": entry.entry_id,
                "tracking_code": ACTIVE_CODE,
                "contents": "Socks",
            },
        )
    add.assert_awaited_once_with(ACTIVE_CODE)
    assert _contents(entry) == "Socks"


async def test_track_parcel_stores_contents_for_a_code_not_listed_yet(hass):
    entry = await _setup(hass)
    with patch(f"{CLIENT}.async_add_to_watchlist", new=AsyncMock()):
        await _call(
            hass,
            "track_parcel",
            {
                "config_entry_id": entry.entry_id,
                "tracking_code": UNLISTED,
                "contents": "Socks",
            },
        )
    assert entry.runtime_data.coordinator.contents_store.get(UNLISTED) == "Socks"


async def test_track_parcel_without_contents_leaves_them_alone(hass):
    entry = await _setup(hass)
    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )
    with (
        patch(f"{CLIENT}.async_add_to_watchlist", new=AsyncMock()),
        patch(f"{CLIENT}.async_get_parcels", new=AsyncMock()) as poll,
    ):
        await _call(
            hass,
            "track_parcel",
            {"config_entry_id": entry.entry_id, "tracking_code": ACTIVE_CODE},
        )
        poll.assert_not_called()
    assert _contents(entry) == "Socks"


async def test_track_parcel_replaces_and_clears_without_a_poll(hass):
    entry = await _setup(hass)
    base = {"config_entry_id": entry.entry_id, "tracking_code": ACTIVE_CODE}
    with (
        patch(f"{CLIENT}.async_add_to_watchlist", new=AsyncMock()),
        patch(f"{CLIENT}.async_get_parcels", new=AsyncMock()) as poll,
        patch.object(AnPostCoordinator, "async_request_refresh") as refresh,
    ):
        await _call(hass, "track_parcel", {**base, "contents": "Socks"})
        assert _contents(entry) == "Socks"
        await _call(hass, "track_parcel", {**base, "contents": "Shoes"})
        assert _contents(entry) == "Shoes"
        await _call(hass, "track_parcel", {**base, "contents": ""})
        assert _contents(entry) is None
        poll.assert_not_called()
        refresh.assert_not_called()


async def test_a_rejected_watchlist_add_stores_nothing(hass):
    entry = await _setup(hass)
    with (
        patch(
            f"{CLIENT}.async_add_to_watchlist",
            new=AsyncMock(side_effect=AnPostApiError("no")),
        ),
        pytest.raises(ServiceValidationError),
    ):
        await _call(
            hass,
            "track_parcel",
            {
                "config_entry_id": entry.entry_id,
                "tracking_code": UNLISTED,
                "contents": "Socks",
            },
        )
    assert entry.runtime_data.coordinator.contents_store.get(UNLISTED) is None


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass)
    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )
    with patch(f"{CLIENT}.async_remove_from_watchlist", new=AsyncMock()):
        await _call(
            hass,
            "untrack_parcel",
            {"config_entry_id": entry.entry_id, "tracking_code": ACTIVE_CODE},
        )
    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None


# --- routing across accounts ------------------------------------------------


async def _two_accounts(hass):
    one = await _setup(hass, [active_sample()], email="one@example.test")
    two = await _setup(hass, [delivered_sample()], email="two@example.test")
    return one, two


async def test_update_contents_goes_only_to_the_account_listing_the_code(hass):
    one, two = await _two_accounts(hass)
    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )
    assert one.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) == "Socks"
    assert two.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None


async def test_update_contents_goes_to_every_account_listing_the_code(hass):
    one = await _setup(hass, [active_sample()], email="one@example.test")
    two = await _setup(hass, [active_sample()], email="two@example.test")
    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )
    for entry in (one, two):
        assert _contents(entry) == "Socks"


async def test_update_contents_needs_an_account_when_none_lists_the_code(hass):
    await _two_accounts(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "update_contents", {"tracking_code": UNLISTED, "contents": "Socks"}
        )
    assert err.value.translation_key == "update_contents_entry_required"


async def test_config_entry_id_targets_exactly_that_account(hass):
    one, two = await _two_accounts(hass)
    await _call(
        hass,
        "update_contents",
        {
            "tracking_code": UNLISTED,
            "contents": "Socks",
            "config_entry_id": two.entry_id,
        },
    )
    assert two.runtime_data.coordinator.contents_store.get(UNLISTED) == "Socks"
    assert one.runtime_data.coordinator.contents_store.get(UNLISTED) is None


async def test_config_entry_id_of_an_unknown_or_unloaded_entry_is_rejected(hass):
    _, two = await _two_accounts(hass)
    assert await hass.config_entries.async_unload(two.entry_id)
    await hass.async_block_till_done()
    for entry_id in (two.entry_id, "missing"):
        with pytest.raises(ServiceValidationError):
            await _call(
                hass,
                "update_contents",
                {"tracking_code": ACTIVE_CODE, "config_entry_id": entry_id},
            )


async def test_an_unloaded_account_is_never_targeted(hass):
    one = await _setup(hass, [active_sample()], email="one@example.test")
    two = await _setup(hass, [active_sample()], email="two@example.test")
    store = two.runtime_data.coordinator.contents_store
    assert await hass.config_entries.async_unload(two.entry_id)
    await hass.async_block_till_done()
    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )
    assert one.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) == "Socks"
    assert store.get(ACTIVE_CODE) is None


# --- store lifecycle --------------------------------------------------------


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass)
    hass_storage[STORE_KEY % entry.entry_id] = {
        "version": 1,
        "key": STORE_KEY % entry.entry_id,
        "data": {"by_code": {ACTIVE_CODE: "Socks"}},
    }
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    with (
        patch(f"{CLIENT}.async_login", new=AsyncMock(return_value={})),
        patch(
            f"{CLIENT}.async_get_parcels",
            new=AsyncMock(return_value=[active_sample()]),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _contents(entry) == "Socks"


async def test_an_entry_without_a_store_works(hass, hass_storage):
    entry = await _setup(hass)
    assert STORE_KEY % entry.entry_id not in hass_storage
    assert _contents(entry) is None


async def test_removing_the_entry_removes_its_store(hass, hass_storage):
    entry = await _setup(hass)
    key = STORE_KEY % entry.entry_id
    hass_storage[key] = {"version": 1, "key": key, "data": {"by_code": {"X": "Y"}}}
    with patch(f"{CLIENT}.async_logout", new=AsyncMock(), create=True):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert key not in hass_storage


async def test_the_store_drops_the_oldest_code_past_the_cap(hass):
    store = ContentsStore(hass, "cap")
    for index in range(CONTENTS_STORE_MAX_CODES + 1):
        store.async_set(f"CODE{index}", "x")
    assert store.get("CODE0") is None
    assert store.get("CODE1") == "x"
    assert store.get(f"CODE{CONTENTS_STORE_MAX_CODES}") == "x"


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass)
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )
    assert events == []


# --- barcode handling -------------------------------------------------------


async def test_contents_survive_reapply_and_poll_for_a_parcel_without_a_barcode(hass):
    barcodeless = active_sample()
    barcodeless["trackingNumber"] = None
    entry = await _setup(hass, [barcodeless, active_sample()])
    coordinator = entry.runtime_data.coordinator
    await _call(
        hass, "update_contents", {"tracking_code": ACTIVE_CODE, "contents": "Socks"}
    )

    def by_barcode():
        return {bool(p["barcode"]): p["contents"] for p in coordinator.data}

    assert by_barcode() == {False: None, True: "Socks"}
    with patch(
        f"{CLIENT}.async_get_parcels",
        new=AsyncMock(return_value=[barcodeless, active_sample()]),
    ):
        await coordinator.async_refresh()
    assert by_barcode() == {False: None, True: "Socks"}
    assert coordinator.known_codes == {ACTIVE_CODE}
