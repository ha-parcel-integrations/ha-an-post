"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping can be tested
as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.an_post.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.an_post.parcels import (
    apply_delivered_filter,
    format_dimensions,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    tracking_url,
)

from .payloads import (
    CATEGORY_CUSTOMS,
    CATEGORY_DELIVERED,
    CATEGORY_DELIVERY_ATTEMPTED,
    CATEGORY_IN_TRANSIT,
    CATEGORY_INFO_RECEIVED,
    CATEGORY_ITEM_RECEIVED,
    CATEGORY_RETURN_TO_SENDER,
    CATEGORY_SORTING,
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
)

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status — item-level 8-category taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category,expected",
    [
        (CATEGORY_IN_TRANSIT, ParcelStatus.IN_TRANSIT),
        (CATEGORY_CUSTOMS, ParcelStatus.IN_TRANSIT),
        (CATEGORY_ITEM_RECEIVED, ParcelStatus.REGISTERED),
        (CATEGORY_SORTING, ParcelStatus.IN_TRANSIT),
        (CATEGORY_DELIVERED, ParcelStatus.DELIVERED),
        (CATEGORY_DELIVERY_ATTEMPTED, ParcelStatus.PROBLEM),
        (CATEGORY_RETURN_TO_SENDER, ParcelStatus.RETURNING),
        (CATEGORY_INFO_RECEIVED, ParcelStatus.REGISTERED),
    ],
)
def test_map_parcel_status_known(category, expected):
    assert map_parcel_status(category) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status(99) == ParcelStatus.UNKNOWN


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status(42) == ParcelStatus.UNKNOWN
    assert map_parcel_status(42) == ParcelStatus.UNKNOWN
    assert caplog.text.count("42") >= 1
    assert caplog.text.count("issues/new") == 1
    assert "issues/new" in caplog.text


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status(77) is None
    assert map_event_status(CATEGORY_DELIVERED) == ParcelStatus.DELIVERED


def test_map_event_status_falls_back_to_trace_code():
    """No webCategoryId on the event: resolve via the recovered traceCode table."""
    # traceCode 14 is filed under "Delivered" (category id 4) in tracking.md.
    assert map_event_status(None, trace_code=14) == ParcelStatus.DELIVERED
    # traceCode 53 is filed under "Return to sender" (category id 6).
    assert map_event_status(None, trace_code=53) == ParcelStatus.RETURNING


def test_map_event_status_unmapped_trace_code_warns_once(caplog):
    assert map_event_status(None, trace_code=9999) is None
    assert map_event_status(None, trace_code=9999) is None
    assert caplog.text.count("9999") == 1
    assert "issues/new" in caplog.text


def test_map_event_status_prefers_category_over_trace_code():
    # A present (even if 0) webCategoryId wins over a traceCode fallback.
    assert map_event_status(CATEGORY_IN_TRANSIT, trace_code=53) == ParcelStatus.IN_TRANSIT


# ---------------------------------------------------------------------------
# timestamp helpers — An Post's naive-local wire format
# ---------------------------------------------------------------------------


def test_parse_iso_handles_offset_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42+00:00").tzinfo is not None
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_normalize_anchors_naive_timestamp_to_dublin():
    """dateDelivered has no offset on the wire — must not be read as UTC."""
    raw = delivered_sample()
    raw["dateDelivered"] = "2026-04-29T13:12:42"  # IST (UTC+1) in late April
    parcel = normalize_parcel(raw)
    assert parcel["delivered_at"] == "2026-04-29T12:12:42+00:00"


def test_normalize_warns_once_on_unparseable_timestamp(caplog):
    raw = active_sample()
    raw["estimatedDeliveryDateTime"] = "29/04/2026 15:00"
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] is None
    assert caplog.text.count("estimatedDeliveryDateTime") >= 1


def test_format_dimensions_needs_all_three_axes():
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


def test_tracking_url_needs_a_code():
    assert tracking_url(None) is None
    assert "AB123456789IE" in tracking_url("AB123456789IE")


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "An Post"
    assert parcel["barcode"] == DELIVERED_CODE
    assert parcel["sender"] == "Example Shop"
    assert parcel["receiver"] == "Jane Doe"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "4"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T12:12:42+00:00"
    # A delivered parcel drops its ETA — the window is meaningless once it has
    # arrived.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["url"] == (
        "https://www.anpost.com/Post-Parcels/Track/Search?trackingNumber="
        + DELIVERED_CODE
    )
    # An Post exposes neither weight nor dimensions.
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None  # no include_history knob — see const.py


def test_normalize_active_parcel_has_a_point_eta_never_a_window():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == "2026-04-29T14:00:00+00:00"
    # An Post's schema has only one ETA field: never a window.
    assert parcel["planned_to"] is None


def test_normalize_never_reports_pickup():
    """An Post's own vocabulary has no "ready for collection" category, and the
    payload carries no pickup-point name/location field."""
    parcel = normalize_parcel(active_sample())
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_normalize_pending_placeholder():
    """A watchlisted-but-not-yet-scanned item still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": "AB000000001IE"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["planned_from"] is None


def test_normalize_blank_and_missing_fields_become_none():
    raw = active_sample()
    raw["retailerName"] = None
    raw["recipientName"] = None
    raw["receiverName"] = None
    parcel = normalize_parcel(raw)
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_normalize_receiver_falls_back_to_receiver_name():
    raw = active_sample()
    raw["recipientName"] = None
    raw["receiverName"] = "Collected By Someone"
    assert normalize_parcel(raw)["receiver"] == "Collected By Someone"


def test_normalize_keeps_raw_payload():
    raw = active_sample()
    assert normalize_parcel(raw)["raw"] is raw


def test_normalize_warns_once_on_unexpected_field(caplog):
    raw = active_sample()
    raw["someBrandNewField"] = "surprise"
    normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("someBrandNewField") == 1


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
