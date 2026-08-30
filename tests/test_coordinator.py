"""Tests for the An Post coordinator: fetching and events.

The parcel mapping itself is covered by ``test_parcels.py``.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.an_post.api import AnPostAuthError
from custom_components.an_post.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_REFRESH_INTERVAL,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    REFRESH_INTERVAL_AUTO,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.an_post.coordinator import (
    AnPostCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _refresh_interval,
    _refresh_setting,
    _stagger_minutes,
)

from .payloads import (
    ACTIVE_CODE,
    CATEGORY_DELIVERY_ATTEMPTED,
    CATEGORY_ITEM_RECEIVED,
    active_sample,
    delivered_sample,
)

EMAIL = "user@example.test"


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={"email": EMAIL, "password": "hunter2"},
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options=options
        or {
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )


def _received(code: str = ACTIVE_CODE) -> dict:
    sample = active_sample(code)
    sample["lastTrackingEventCategoryId"] = CATEGORY_ITEM_RECEIVED
    return sample


def _delivery_attempted(code: str = ACTIVE_CODE) -> dict:
    sample = active_sample(code)
    sample["lastTrackingEventCategoryId"] = CATEGORY_DELIVERY_ATTEMPTED
    return sample


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_splits_active_and_delivered(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [active_sample(), delivered_sample()]
    coordinator = AnPostCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert [parcel["barcode"] for parcel in data] == [ACTIVE_CODE]
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_handles_an_empty_account(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = []
    coordinator = AnPostCoordinator(hass, client, entry)

    assert await coordinator._async_update_data() == []


async def test_expired_session_triggers_reauth(hass):
    """An expired session must start reauth, not retry forever."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.side_effect = AnPostAuthError("HTTP 401")
    coordinator = AnPostCoordinator(hass, client, entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    """Otherwise every restart floods the user with "registered" events."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [active_sample()]
    coordinator = AnPostCoordinator(hass, client, entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = AsyncMock()
    coordinator = AnPostCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [_received()]
    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [active_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_fires_status_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = AnPostCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [_received()]
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_parcels.return_value = [_delivery_attempted()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.REGISTERED
    assert events[0].data["new_status"] == ParcelStatus.PROBLEM


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    """The hop to delivered fires exactly one, dedicated event."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = AnPostCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcels.return_value = [active_sample()]
    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [delivered_sample(ACTIVE_CODE)]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when it first appears fires nothing."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = AnPostCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    client.async_get_parcels.return_value = [active_sample()]
    await coordinator._async_update_data()  # first refresh seeds the state
    client.async_get_parcels.return_value = [active_sample(), delivered_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = AnPostCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    client.async_get_parcels.return_value = [active_sample()]
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_parcels.return_value = [
        active_sample(),
        active_sample("CD888888888IE"),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == "CD888888888IE"


async def test_fires_delivery_time_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = AnPostCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [active_sample()]
    await coordinator._async_update_data()  # first refresh: suppressed

    moved = active_sample()
    moved["estimatedDeliveryDateTime"] = "2026-04-29T18:00:00"
    client.async_get_parcels.return_value = [moved]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_planned_from"] == "2026-04-29T17:00:00+00:00"


async def test_losing_the_eta_is_silent(hass):
    """value -> null just means the carrier lost the window; not worth an alert."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = AnPostCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [active_sample()]
    await coordinator._async_update_data()

    dropped = active_sample()
    dropped["estimatedDeliveryDateTime"] = None
    client.async_get_parcels.return_value = [dropped]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


# ---------------------------------------------------------------------------
# _refresh_interval / _refresh_setting
# ---------------------------------------------------------------------------


def test_refresh_interval_defaults_to_30_minutes_when_option_unset():
    entry = _entry(options={})
    assert _refresh_interval(entry).total_seconds() == 30 * 60


def test_refresh_interval_reads_from_options():
    entry = _entry(options={CONF_REFRESH_INTERVAL: 60})
    assert _refresh_interval(entry).total_seconds() == 60 * 60


def test_refresh_interval_starts_hot_when_auto():
    entry = _entry(options={CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    assert _refresh_interval(entry).total_seconds() == HOT_INTERVAL_MINUTES * 60


def test_refresh_setting_passes_through_auto():
    entry = _entry(options={CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    assert _refresh_setting(entry) == REFRESH_INTERVAL_AUTO


# ---------------------------------------------------------------------------
# Dynamic polling (Section 2.2, account-based) — pure helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def test_quiet_window_is_midnight_to_six():
    assert _in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert _in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 23, 59, tzinfo=UTC))


def test_next_anchor_before_six_is_six_today():
    now = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_next_anchor_after_six_is_midnight_tomorrow():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_stagger_is_stable_and_bounded():
    a = _stagger_minutes("entry-1")
    b = _stagger_minutes("entry-1")
    c = _stagger_minutes("entry-2")
    assert a == b
    assert 0 <= a < STAGGER_MINUTES
    assert 0 <= c < STAGGER_MINUTES


def test_tier_is_mid_when_nothing_active():
    assert _hottest_tier_minutes([], datetime(2026, 1, 1, 12, tzinfo=UTC)) == MID_INTERVAL_MINUTES


def test_tier_is_mid_for_non_hot_statuses():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": "registered", "planned_from": None},
        {"status": "problem", "planned_from": None},
        {"status": "returning", "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_tier_is_hot_when_out_for_delivery_without_planned_from():
    # An Post's own status map never actually produces out_for_delivery (no
    # same-day-delivery category exists on this carrier), but the shared
    # algorithm is implemented generically for forward-compatibility — build
    # the dict by hand to exercise the branch.
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": "in_transit", "planned_from": None},
        {"status": "out_for_delivery", "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_when_planned_from_is_unparseable():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [{"status": "out_for_delivery", "planned_from": "not-a-date"}]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_within_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(minutes=30)  # inside the 1h lookahead
    parcels = [{"status": "out_for_delivery", "planned_from": planned.isoformat()}]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_before_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(hours=3)  # well outside the 1h lookahead
    parcels = [{"status": "out_for_delivery", "planned_from": planned.isoformat()}]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_daytime_candidate_outside_window_is_tier_plus_stagger():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    stagger = _stagger_minutes("entry-1")
    assert interval == timedelta(minutes=MID_INTERVAL_MINUTES + stagger)


def test_now_inside_quiet_window_jumps_to_next_anchor():
    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # an anchor poll itself
    interval = _next_update_interval(now, HOT_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_candidate_landing_in_quiet_window_clamps_to_the_midnight_anchor():
    now = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Dynamic polling — wired into _async_update_data
# ---------------------------------------------------------------------------


async def test_auto_mode_recomputes_interval_and_never_stops(hass):
    """Zero pending parcels must not suspend polling — it's the only discovery path."""
    entry = _entry(options={CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = []
    coordinator = AnPostCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coordinator.update_interval is not None


async def test_auto_mode_goes_hot_for_out_for_delivery(hass):
    entry = _entry(options={CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO})
    entry.add_to_hass(hass)
    client = AsyncMock()
    sample = active_sample()
    sample["lastTrackingEventCategoryId"] = CATEGORY_ITEM_RECEIVED
    client.async_get_parcels.return_value = [sample]
    coordinator = AnPostCoordinator(hass, client, entry)

    # An Post's own status map never yields out_for_delivery, so force the
    # tier calculation to prove the hot branch is wired up rather than
    # relying on a real payload to reach that status.
    with patch(
        "custom_components.an_post.coordinator._hottest_tier_minutes",
        return_value=HOT_INTERVAL_MINUTES,
    ):
        await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == HOT_INTERVAL_MINUTES


async def test_fixed_mode_keeps_configured_interval(hass):
    entry = _entry(options={CONF_REFRESH_INTERVAL: 60})
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = []
    coordinator = AnPostCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes is None
    assert coordinator.update_interval == timedelta(minutes=60)
