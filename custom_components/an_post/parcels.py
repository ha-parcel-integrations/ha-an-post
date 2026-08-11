"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That keeps the carrier-specific
mapping apart from the coordinator (which is nearly identical everywhere) and
makes the mapping trivially unit-testable without spinning up HA.

Carrier-specific here: :data:`_CATEGORY_STATUS`, :data:`_TRACE_CODE_CATEGORY`,
:func:`normalize_parcel` and :func:`_parse_an_post_timestamp`. Everything
else — the sort contract, the delivered filter — is suite-wide machinery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status/field we do not map yet.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-an-post/issues/new"
    "?template=unrecognised_status.yml"
)

# An Post has one timezone. Its own wire timestamps (``estimatedDeliveryDateTime``,
# ``dateDelivered``, each event's ``date``/``scanDate``) carry no offset at all —
# resolved at import (never in the event loop) so a naive stamp can be anchored
# to Dublin rather than silently read as UTC.
_DUBLIN = ZoneInfo("Europe/Dublin")

# The exact wire format confirmed from the app's date parser: no timezone, no
# fractional seconds. `Locale.ENGLISH`, matched here with strptime.
_WIRE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

# An Post's own item-level taxonomy — ``lastTrackingEventCategoryId`` (and,
# reused verbatim, each event's ``webCategoryId``). Corrected against the
# canonical ``ParcelStatus`` enum: the carrier's own labels ("Customs",
# "Delivery attempted") are not canonical values, so each folds into the
# nearest of the eight. "Customs" -> IN_TRANSIT mirrors how every other suite
# carrier treats a bare customs-clearance category (still moving through the
# network, not a problem on its own) — see ha-dhl-nl / ha-swiss-post /
# ha-oesterreichische-post. An Post's own vocabulary has no locker/"ready for
# collection" category, so AT_PICKUP_POINT is never produced by this map.
_CATEGORY_STATUS: dict[int, ParcelStatus] = {
    0: ParcelStatus.IN_TRANSIT,  # In transit
    1: ParcelStatus.IN_TRANSIT,  # Customs
    2: ParcelStatus.REGISTERED,  # Item received
    3: ParcelStatus.IN_TRANSIT,  # Sorting
    4: ParcelStatus.DELIVERED,  # Delivered
    5: ParcelStatus.PROBLEM,  # Delivery attempted
    6: ParcelStatus.RETURNING,  # Return to sender
    7: ParcelStatus.REGISTERED,  # Info received
}

# traceCode -> category id, recovered whole from the app (tracking.md). Used
# only as an event-level fallback when a ``TrackingItemHistory`` entry carries
# a ``traceCode`` but no ``webCategoryId`` — every field on it is boxed/optional.
_TRACE_CODE_CATEGORY: dict[int, int] = {
    code: category
    for category, codes in {
        3: (1, 8, 19, 46, 51, 52, 57, 78, 85),
        2: (6, 15, 32, 36, 37, 41, 48),
        0: (4, 5, 40, 43, 44, 45, 47, 49, 50, 63, 67, 68, 69, 70, 73, 79),
        1: (7, 9, 64, 65, 66, 83, 86, 87),
        5: (13, 16),
        4: (14, 42),
        7: (35,),
        6: (53, 55, 56, 71, 75, 76, 77),
    }.items()
    for code in codes
}

# Every ``TrackingItemDetailsV2`` field the app's Moshi model declares
# (tracking.md). A key outside this set on a real ``trackingItems`` item means
# the response is richer than what research recovered from the app — schema
# drift worth a one-shot report.
_EXPECTED_ITEM_FIELDS = frozenset(
    {
        "trackingNumber",
        "alias",
        "lastTrackingEventCategoryId",
        "progress",
        "estimatedDeliveryDateTime",
        "dateDelivered",
        "retailerName",
        "retailerId",
        "recipientName",
        "receiverName",
        "receiverNameTrunc",
        "recipientAddress",
        "destinationCountyCity",
        "destinationCountry",
        "deliveryPin",
        "parcelLockerPin",
        "orderNumber",
        "mrn",
        "customsPaymentRequired",
        "customsPaymentTotalDue",
        "customsPaymentDateDue",
        "isPodSignatureAvailable",
        "isSafePlacePhotoAvailable",
        "returnType",
        "isHistorical",
    }
)

# One-shot warning bookkeeping — each set is logged into at most once per
# distinct value per HA session, so a chatty account never floods the log.
_unmapped_category_logged: set[int] = set()
_unmapped_trace_code_logged: set[int] = set()
_unexpected_fields_logged: set[str] = set()
_unexpected_format_logged: set[str] = set()


def _warn_unmapped_category(category_id: int) -> None:
    if category_id in _unmapped_category_logged:
        return
    _unmapped_category_logged.add(category_id)
    _LOGGER.warning(
        "Unrecognised An Post tracking-event category — help us map it. Open "
        "an issue and paste this line: %s\n"
        "  lastTrackingEventCategoryId/webCategoryId=%s -> reported as 'unknown'",
        NEW_ISSUE_URL,
        category_id,
    )


def _warn_unmapped_trace_code(trace_code: int) -> None:
    if trace_code in _unmapped_trace_code_logged:
        return
    _unmapped_trace_code_logged.add(trace_code)
    _LOGGER.warning(
        "Unrecognised An Post traceCode — help us map it. Open an issue and "
        "paste this line: %s\n  traceCode=%s -> reported as 'unknown'",
        NEW_ISSUE_URL,
        trace_code,
    )


def _warn_unexpected_fields(fields: set[str]) -> None:
    new = fields - _unexpected_fields_logged
    if not new:
        return
    _unexpected_fields_logged.update(new)
    _LOGGER.warning(
        "An Post's trackingItems payload carries field(s) we do not map yet "
        "— the response may be richer than what was recovered from the app. "
        "Please help us confirm it by opening an issue and pasting this "
        "line: %s\n  fields=%s",
        NEW_ISSUE_URL,
        sorted(new),
    )


def _warn_unexpected_format(field: str, value: Any) -> None:
    if field in _unexpected_format_logged:
        return
    _unexpected_format_logged.add(field)
    _LOGGER.warning(
        "An Post reported %s as %r, which does not match the "
        "yyyy-MM-dd'T'HH:mm:ss format recovered from the app — the value is "
        "dropped rather than guessed at. Please help us fix the parser by "
        "opening an issue and pasting this line: %s",
        field,
        value,
        NEW_ISSUE_URL,
    )


def map_parcel_status(category_id: int | None) -> ParcelStatus:
    """Map ``lastTrackingEventCategoryId`` to a canonical :class:`ParcelStatus`.

    ``None`` (a watchlisted-but-not-yet-scanned parcel) reports ``unknown``
    silently; an unrecognised id reports ``unknown`` with a one-shot warning.
    """
    if category_id is None:
        return ParcelStatus.UNKNOWN
    mapped = _CATEGORY_STATUS.get(category_id)
    if mapped is not None:
        return mapped
    _warn_unmapped_category(category_id)
    return ParcelStatus.UNKNOWN


def map_event_status(
    category_id: int | None, trace_code: int | None = None
) -> ParcelStatus | None:
    """Map one history event to a canonical status, or ``None``.

    Prefers the event's own ``webCategoryId`` (same table as the item level);
    falls back to resolving ``traceCode`` through its category when the
    category id itself is missing — every ``TrackingItemHistory`` field is
    boxed/optional. Unmapped values keep ``status: null`` (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once each.
    """
    if category_id is None and trace_code is not None:
        resolved = _TRACE_CODE_CATEGORY.get(trace_code)
        if resolved is None:
            _warn_unmapped_trace_code(trace_code)
            return None
        category_id = resolved

    if category_id is None:
        return None
    mapped = _CATEGORY_STATUS.get(category_id)
    if mapped is not None:
        return mapped
    _warn_unmapped_category(category_id)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Used on the canonical (already-normalised) timestamps this module
    produces, all of which carry an explicit UTC offset — see
    :func:`_parse_an_post_timestamp`.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_an_post_timestamp(value: Any, *, field: str) -> str | None:
    """Parse one of An Post's own wire timestamps into a canonical ISO string.

    The API emits ``yyyy-MM-dd'T'HH:mm:ss`` — no timezone, no fractional
    seconds (APK-confirmed, ``Locale.ENGLISH``). Naive values are anchored to
    Europe/Dublin (An Post's only timezone) and converted to UTC. A value that
    does not match this exact shape self-reports once (pre-1.0: the format is
    confirmed from the app but has never been seen on a populated parcel) and
    is dropped rather than guessed at.
    """
    if not value:
        return None
    try:
        naive = datetime.strptime(str(value), _WIRE_DATETIME_FORMAT)
    except ValueError:
        _warn_unexpected_format(field, value)
        return None
    return naive.replace(tzinfo=_DUBLIN).astimezone(timezone.utc).isoformat()


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"``. An Post exposes neither weight nor dimensions
    (tracking.md), so this is never actually populated for this carrier — kept
    for parity with the rest of the suite and in case that ever changes.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def normalize_parcel(raw: dict) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order.

    An Post-specific notes:

    * Every ``TrackingItemDetailsV2`` field is a **boxed** (nullable) type —
      nothing is guaranteed present except ``trackingNumber`` — so every
      lookup below is guarded.
    * ``status`` comes from ``lastTrackingEventCategoryId`` via
      :func:`map_parcel_status`; the carrier's own category has no
      human-readable text, so ``raw_status`` falls back to the numeric id
      as a string.
    * ``estimatedDeliveryDateTime`` is a single instant, not a window — An
      Post's schema has no separate "to" field, so ``planned_to`` is always
      ``None`` (a point estimate, per the units contract).
    * ``pickup`` / ``pickup_point`` are always ``False`` / ``None``: An
      Post's own status vocabulary has no "ready for collection" category and
      the payload carries no pickup-point name/location field, even though
      ``deliveryPin`` / ``parcelLockerPin`` hint that locker delivery exists.
    * ``weight`` / ``dimensions`` are always ``None`` — the API exposes
      neither (tracking.md).
    * ``history`` is always ``None`` — see the note on
      ``CONF_INCLUDE_HISTORY``'s absence in const.py.
    * ``receiver`` prefers ``recipientName`` (the label's addressee) and
      falls back to ``receiverName`` (who actually took delivery, populated
      once collected).
    """
    _warn_unexpected_fields(set(raw) - _EXPECTED_ITEM_FIELDS)

    tracking_code = raw.get("trackingNumber")
    category_id = raw.get("lastTrackingEventCategoryId")
    status = map_parcel_status(category_id)
    delivered = status is ParcelStatus.DELIVERED

    planned_from = None
    if not delivered:
        planned_from = _parse_an_post_timestamp(
            raw.get("estimatedDeliveryDateTime"), field="estimatedDeliveryDateTime"
        )

    return {
        "carrier": "An Post",
        "barcode": tracking_code,
        "sender": raw.get("retailerName") or None,
        "receiver": raw.get("recipientName") or raw.get("receiverName") or None,
        "status": status,
        "raw_status": (
            str(category_id) if category_id is not None else None
        ),
        "delivered": delivered,
        "delivered_at": (
            _parse_an_post_timestamp(raw.get("dateDelivered"), field="dateDelivered")
            if delivered
            else None
        ),
        "planned_from": planned_from,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
