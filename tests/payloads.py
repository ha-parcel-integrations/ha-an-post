"""Sample An Post API payloads shared by the test modules.

Shaped after ``TrackingItemDetailsV2`` — the fields, nullability and wire date
format APK-confirmed in carrier-research/api/an-post/tracking.md. No live
payload has been captured yet (the test account had no parcel), so these are
reconstructed from the app's models rather than redacted from the wire; the
runtime *values* (is ``estimatedDeliveryDateTime`` ever populated, the literal
free text) are the one open question — see the module's pre-1.0 WARNINGs.
"""
from __future__ import annotations

ACTIVE_CODE = "AB999999999IE"
DELIVERED_CODE = "AB123456789IE"

# lastTrackingEventCategoryId values (also each event's webCategoryId) —
# An Post's own item-level taxonomy, see tracking.md's status vocabulary.
CATEGORY_IN_TRANSIT = 0
CATEGORY_CUSTOMS = 1
CATEGORY_ITEM_RECEIVED = 2
CATEGORY_SORTING = 3
CATEGORY_DELIVERED = 4
CATEGORY_DELIVERY_ATTEMPTED = 5
CATEGORY_RETURN_TO_SENDER = 6
CATEGORY_INFO_RECEIVED = 7


def event(
    web_category_id: int | None,
    date: str,
    activity: str,
    *,
    trace_code: int | None = None,
) -> dict:
    """One entry of An Post's own ``TrackingItemHistory``/``ScanEvent`` shape."""
    return {
        "date": date,
        "activity": activity,
        "webCategoryId": web_category_id,
        "traceCode": trace_code,
        "location": "Dublin Mail Centre",
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative ``TrackingItemDetailsV2`` for a delivered parcel."""
    return {
        "trackingNumber": code,
        "alias": None,
        "lastTrackingEventCategoryId": CATEGORY_DELIVERED,
        "progress": 100,
        "retailerName": "Example Shop",
        "retailerId": "RET1",
        "recipientName": "Jane Doe",
        "receiverName": "Jane Doe",
        "receiverNameTrunc": "Jane D.",
        "recipientAddress": "1 Example Street, Dublin",
        "destinationCountyCity": "Dublin",
        "destinationCountry": "IE",
        "estimatedDeliveryDateTime": None,
        "dateDelivered": "2026-04-29T13:12:42",
        "deliveryPin": None,
        "parcelLockerPin": None,
        "orderNumber": "ORD-1",
        "mrn": None,
        "customsPaymentRequired": False,
        "customsPaymentTotalDue": None,
        "customsPaymentDateDue": None,
        "isPodSignatureAvailable": True,
        "isSafePlacePhotoAvailable": False,
        "returnType": None,
        "isHistorical": False,
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An in-transit parcel with an ETA instant."""
    sample = delivered_sample(code)
    sample.update(
        {
            "lastTrackingEventCategoryId": CATEGORY_IN_TRANSIT,
            "dateDelivered": None,
            "estimatedDeliveryDateTime": "2026-04-29T15:00:00",
        }
    )
    return sample
