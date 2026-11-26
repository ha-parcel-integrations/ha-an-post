"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime

import pytest

from custom_components.an_post.const import ParcelStatus
from custom_components.an_post.parcels import is_pickup, normalize_parcel, parse_iso

from .payloads import (
    CATEGORY_DELIVERY_ATTEMPTED,
    CATEGORY_RETURN_TO_SENDER,
    active_sample,
    delivered_sample,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def _with_category(category: int) -> dict:
    sample = active_sample()
    sample["lastTrackingEventCategoryId"] = category
    return sample


def _delivered_with_estimate() -> dict:
    sample = delivered_sample()
    sample["estimatedDeliveryDateTime"] = "2026-04-29T15:00:00"
    return sample


def _utc_edge() -> dict:
    sample = active_sample()
    sample["estimatedDeliveryDateTime"] = "2026-07-01T23:30:00"
    return sample


SAMPLES = {
    "delivered": delivered_sample,
    "delivered_with_estimate": _delivered_with_estimate,
    "active": active_sample,
    "returning": lambda: _with_category(CATEGORY_RETURN_TO_SENDER),
    "problem": lambda: _with_category(CATEGORY_DELIVERY_ATTEMPTED),
    "late_evening": _utc_edge,
    "bare": lambda: {"trackingNumber": "AB000000001IE"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_parcel_without_a_locker_pin_has_no_pickup_route():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_moment_ends_at_the_end_of_that_local_day():
    parcel = normalize_parcel(active_sample())
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == start.date()


def test_a_moment_late_in_the_evening_keeps_the_dublin_day():
    parcel = normalize_parcel(_utc_edge())
    assert parcel["planned_from"] == "2026-07-01T23:30:00+01:00"
    assert parcel["planned_to"] == "2026-07-01T23:59:59+01:00"


def test_returning_keeps_the_delivered_flag_false():
    parcel = normalize_parcel(_with_category(CATEGORY_RETURN_TO_SENDER))
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_is_pickup_helper_survives_collection():
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(True, ParcelStatus.IN_TRANSIT)
    assert not is_pickup(None, ParcelStatus.IN_TRANSIT)
