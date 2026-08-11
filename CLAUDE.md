# Working in this repository

Home Assistant custom integration for **An Post** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
Account-based (Gigya/SAP Customer Data Cloud login) with a bare-number
watchlist add on top — see *Carrier-specific notes* below. No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific notes

**API mechanics live in `carrier-research/api/an-post/` (private research
repo)** — the Gigya auth chain, `my-deliveries-api`'s endpoints, the response
envelope/nullability, the wire date format, and both status vocabularies. Do
not duplicate them here; this file is HA-integration decisions only.

- **Two-layer auth, not the suite's usual cookie login.** `api.py:AnPostAuth`
  owns the Gigya session (`{UID, login_token}`) separately from
  `AnPostApiClient`, which never talks to Gigya directly. `async_id_token()`
  mints a fresh, short-lived `id_token` **every poll** rather than caching it,
  and re-runs `accounts.login` once with the stored password when `getJWT`
  reports `errorCode 403007` (session expired) — that retry is internal to
  the client; the coordinator only ever sees `AnPostAuthError` /
  `AnPostApiError` like every other carrier. **Gigya answers every call with
  HTTP 200** — every branch is on the body's `errorCode`, never on the HTTP
  status; only `my-deliveries-api` itself uses real HTTP status codes
  (401/403 → `AnPostAuthError`).
- **`GIGYA_API_KEY` is a public Gigya site key** (ships in anpost.com's own
  JS) — the Canada Post `client_id` class, not a secret. Nothing else in the
  chain is shared; the user brings their own e-mail + password.
- **The item-level status map is corrected against the canonical enum, not
  copied from An Post's own labels.** An Post's `lastTrackingEventCategoryId`
  taxonomy has "Customs" and "Delivery attempted" categories that are not
  `ParcelStatus` values — "Customs" folds into `IN_TRANSIT` (mirrors
  ha-dhl-nl / ha-swiss-post / ha-oesterreichische-post's treatment of a bare
  customs-clearance status: still moving through the network, not a problem
  on its own) and "Delivery attempted" folds into `PROBLEM`. **An Post's own
  vocabulary has no locker/"ready for collection" category and the payload
  carries no pickup-point name/location field** — `pickup` and
  `pickup_point` are therefore always `False` / `None`; `AT_PICKUP_POINT` is
  never produced by this carrier's map, and neither is `OUT_FOR_DELIVERY`
  (no same-day-delivery category exists either).
- **Event-level status has a two-step fallback.** Each `TrackingItemHistory`
  entry's own `webCategoryId` reuses the same 0–7 map as the item level; when
  it is missing (every field is boxed/optional) `map_event_status` falls back
  to resolving the event's `traceCode` through `_TRACE_CODE_CATEGORY`
  (recovered whole from the app) before giving up and warning. A present
  `webCategoryId`, even `0`, always wins over the `traceCode` fallback.
- **No `include_history` option, unlike every other account-based carrier in
  the suite.** The event timeline (`itemHistoryList[]`) is not part of the
  `trackingitems` inbox response at all — it only exists on the per-parcel
  *detail* call (`GET .../trackingitems/{trackingNumber}`), and that
  response's envelope has never been captured live (research recovered the
  item fields from the app's Moshi models, not a real detail call). Building
  the enrichment against a guessed envelope risks silently wrong history —
  see ha-budbee for the same call ("a toggle that can never safely populate
  is worse than no toggle"). `history` is always `None`. Revisit once a real
  detail response is seen and its top-level shape is confirmed.
- **Naive timestamps are anchored to `Europe/Dublin`, not UTC.** An Post's
  wire format (`yyyy-MM-dd'T'HH:mm:ss`, confirmed from the app's date
  parser) carries no offset at all. `_DUBLIN`/`_WIRE_DATETIME_FORMAT` are
  resolved/defined at import, never in the event loop. A value that does not
  match that exact shape self-reports once (pre-1.0: the format is
  APK-confirmed but has never been seen on a populated parcel) and the field
  is dropped rather than guessed at.
- **`estimatedDeliveryDateTime` is a single instant, never a window** — An
  Post's schema has no separate "to" field, so `planned_to` is always `None`.
- **`receiver` prefers `recipientName`** (the label's addressee) **and falls
  back to `receiverName`** (who actually took delivery, populated once
  collected). `weight` / `dimensions` are always `None` — the API exposes
  neither.
- **Pre-1.0 self-reporting**, since the payload shape is APK-confirmed but no
  *populated* body has ever been seen (the test account had no parcel): a
  one-shot WARNING with the issue-template link fires the first time a real
  `trackingItems` item carries a field outside `_EXPECTED_ITEM_FIELDS`
  (schema drift), the first time `estimatedDeliveryDateTime` or
  `dateDelivered` fails the wire-format parse, and — same one-shot machinery
  as every other carrier — the first time an unmapped
  `lastTrackingEventCategoryId` / `webCategoryId` / `traceCode` is seen.
  Remove these once a real parcel has confirmed the runtime values.
- **The watchlist services call the *live* API, not local config-entry
  storage** — unlike every account-less carrier's `track_parcel`. A
  watchlisted number is folded into the account's own `trackingitems`
  response by An Post itself, so the coordinator needs no extra plumbing to
  pick it up; `services.py` just calls
  `AnPostApiClient.async_add_to_watchlist` / `async_remove_from_watchlist`.
  Because more than one An Post account can be configured (`manifest.json`
  has no `single_config_entry`, unlike the account-less carriers), both
  services take a `config_entry_id` (`selector.ConfigEntrySelector`) rather
  than assuming a single hub — see `services.py:_resolve_client`. Services
  are registered on first entry setup and only unregistered once the last
  account unloads (`__init__.py`).
- **Tracking-code format**: An Post's S10 shape,
  `^[A-Z]{2}\d{9}IE$` (`const.py:TRACKING_CODE_PATTERN`) — validated in
  `services.py` before the watchlist call is made.

## Options and reloads

The options flow is one sectioned form (`data_entry_flow.section`); changes apply
without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default) apply changes live: an update listener
  retunes `coordinator.update_interval` and calls `async_request_refresh()`, so
  added/removed parcel sensors appear immediately.
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+.

The user-tunable poll interval is a deliberate HACS divergence (see
CONVENTIONS.md); a carrier that throttles is generated with a fixed cadence and no
polling option at all.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (HTTP client, error types) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (URLs) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (`_STATUS_MAP`, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` | partly (code validation) |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (watchlist add/remove; here calls the live API — see *Carrier-specific notes*) | partly |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Tests on Windows

`tests/conftest.py` carries two Windows-only shims (no-ops elsewhere):
`disable_socket` is neutralised (Windows event loops need AF_INET socketpairs;
the 127.0.0.1 allowlist stays) and HA's `AsyncResolver` is swapped for
`ThreadedResolver` (aiodns refuses the Proactor loop). Do not remove them
"because CI passes" — CI is Linux, development is Windows.

## Running tests

```
python -m pytest tests/ --cov=custom_components.an_post
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in this carrier's directory under the private
`carrier-research/api/`, never in this repo.
