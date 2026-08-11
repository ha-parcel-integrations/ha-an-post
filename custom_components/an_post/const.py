"""Constants for the An Post parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "an_post"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping this carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields this carrier's API actually populates — feeds
# the comparison table on the docs site. Keep in lockstep with
# normalize_parcel() in parcels.py: everything not listed here comes back as a
# literal None there. An Post exposes a single estimated-delivery instant (no
# window end, no pickup-point name, no weight/dimensions, and no history route).
CAPABILITIES = frozenset({"delivery_window", "url"})

# Full write-up: carrier-research/api/an-post/{login,tracking}.md (private
# research repo). Kept here only as much as the code needs to act on it.
#
# --- Gigya (SAP Customer Data Cloud) auth chain ---------------------------
# An Post authenticates through Gigya, not through my-deliveries-api itself.
# ``GIGYA_API_KEY`` is a public Gigya *site* key (ships in anpost.com's own
# JS) — the Canada Post client_id class, not a secret. Nothing else in the
# chain is shared: the user brings their own e-mail + password.
GIGYA_HOST = "https://identity.anpost.com"
GIGYA_API_KEY = "4_-hnpKBjR2UeyQkTv_-11Pw"
GIGYA_BOOTSTRAP_URL = f"{GIGYA_HOST}/accounts.webSdkBootstrap"
GIGYA_LOGIN_URL = f"{GIGYA_HOST}/accounts.login"
GIGYA_GET_JWT_URL = f"{GIGYA_HOST}/accounts.getJWT"

# Gigya answers every call with HTTP 200 and reports the real outcome in the
# body's ``errorCode`` — branch on that, never on the HTTP status.
GIGYA_ERROR_OK = 0
GIGYA_ERROR_INVALID_CREDENTIALS = 403042  # accounts.login: wrong e-mail/password
GIGYA_ERROR_PERMISSION_DENIED = 403007  # accounts.getJWT: login_token/session expired

# --- my-deliveries-api ------------------------------------------------------
# Every call carries ``Authorization: <id_token>`` (no "Bearer" prefix) minted
# fresh each poll from the Gigya session — see api.py:AnPostAuth.
MY_DELIVERIES_BASE = (
    "https://apim-anpost-mydeliveries.anpost.com/an-post-my-deliveries-api/v2"
)
TRACKING_ITEMS_URL = MY_DELIVERIES_BASE + "/api/customer/{uid}/trackingitems"
WATCHLIST_ITEM_URL = (
    MY_DELIVERIES_BASE + "/api/customer/{uid}/watchlist/{tracking_number}"
)

# The consumer-facing tracking page — used for the deep link on ``url``.
TRACKING_URL = "https://www.anpost.com/Post-Parcels/Track/Search?trackingNumber={tracking_code}"

# An Post's own tracking-number format (S10): two letters, nine digits, "IE".
# Used to validate a bare number before it is added to the account's
# server-side watchlist (see services.py).
TRACKING_CODE_PATTERN = r"^[A-Z]{2}\d{9}IE$"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls the
# carrier. Default 30 min keeps the load on a consumer endpoint gentle; the
# minimum is 15 min for the same reason.
#
# Deliberate divergence from the HA Core rule that polling intervals are not
# user-configurable: that rule targets core integrations, and in a HACS parcel
# tracker a tunable cadence is a wanted feature. Generate with
# ``--interval fixed`` instead when the carrier throttles or soft-bans unusual
# traffic.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

# No ``include_history`` option here, unlike every other carrier in the suite.
# The event timeline (``TrackingItemHistory`` / ``ScanEvent``) is not part of
# the ``trackingitems`` inbox response at all — it only exists on the
# per-parcel *detail* call (``ItemDetailsResponse.itemHistoryList``), and that
# response's own envelope has never been captured (research recovered the
# item fields from the app's models, not a live detail call). Building the
# opt-in enrichment against a guessed envelope risks silently wrong history —
# a toggle that can never safely populate is worse than no toggle (see
# ha-budbee). Revisit once a real detail response is seen.
