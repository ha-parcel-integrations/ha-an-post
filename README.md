# An Post Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-an-post.svg)](https://github.com/ha-parcel-integrations/ha-an-post/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

> ⚠️ **Pre-1.0 release.** The login → `my-deliveries-api` chain is live-validated end-to-end and the payload shape is confirmed from the app's own code, but no *populated* response has been seen yet (the account used to build this had no parcel). Unknown statuses log a one-shot `WARNING` with a link to report them — see [Troubleshooting](#troubleshooting). If you get a real An Post parcel, a diagnostics dump after it updates is the single most useful thing you can contribute.

A custom Home Assistant integration that tracks your [An Post](https://www.anpost.com) parcels. Sign in with your own An Post account (the same one you use on [anpost.com](https://www.anpost.com) or the *An Post: Track & Manage* app) and every parcel it already knows about is imported automatically; a parcel someone else sent you can be added to the account's watchlist from Home Assistant too.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Auto-imports every parcel your An Post account already knows about — no per-parcel setup
- `an_post.track_parcel` / `an_post.untrack_parcel` services add or remove a bare tracking number from the account's own watchlist, for parcels the account has not linked yet
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `delivered` / `returning` / `problem` / …), An Post's own category id, the expected delivery moment and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery moments
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Automatic re-authentication when the An Post session expires
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.7 or newer
- An [An Post](https://www.anpost.com) account (the same login as the website or the *Track & Manage* app)

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-an-post` as an **Integration**.
3. Install **An Post** and restart Home Assistant.

### Manual

Copy `custom_components/an_post` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → An Post**, then enter your An Post account's e-mail address and password. The integration signs in through the same identity service the app and website use and imports every parcel already on the account.

To add a parcel the account has not linked yet, use the [`an_post.track_parcel`](#services) service (or a [dashboard button](examples/dashboards/add_parcel_card.yaml)) — it is added to the account's own watchlist, so it also shows up if you open the An Post app.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Polling | Refresh every | 30 min | How often An Post is checked. Slower is gentler on their API. |

Changing an option reloads the integration; there is no restart needed.

## Removal

Standard HA removal applies: **Settings → Devices & Services → An Post → ⋮ → Delete**. Nothing extra is required on An Post's side — the integration only reads the account.

## Sensors

The integration creates one device per An Post account, named **`An Post (<your-email>)`**. With multiple accounts each gets its own device.

| Friendly name pattern | Description |
|---|---|
| `An Post (account) Incoming parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `An Post (account) Parcel <barcode>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `An Post (account) Next delivery` | Earliest expected delivery moment across all active parcels |
| `An Post (account) Delivered parcels` | Recently delivered parcels (see the retention option) |
| `An Post (account) Last successful update` | Diagnostic: when An Post was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

Every parcel exposed on a sensor attribute uses a carrier-agnostic shape:

| Key | Type | Meaning |
|---|---|---|
| `carrier` | string | `"An Post"` |
| `barcode` | string | Parcel tracking number |
| `sender` | string \| null | Retailer name, when known |
| `receiver` | string \| null | Recipient name |
| `status` | `ParcelStatus` | Canonical status — see the [status reference](#parcel-status-reference) |
| `raw_status` | string \| null | An Post's own numeric tracking-event category (power users) |
| `delivered` | bool | Whether the parcel has been delivered |
| `delivered_at` | ISO 8601 \| null | Delivery moment, if known |
| `planned_from` | ISO 8601 \| null | Expected delivery moment. An Post reports a single instant, never a window, so this is the only ETA field that is ever populated. |
| `planned_to` | ISO 8601 \| null | Always `null` — An Post's schema has no separate window end. |
| `pickup` | bool | Always `false` — An Post's own status vocabulary has no "ready for collection" category. |
| `pickup_point` | string \| null | Always `null` — the payload carries no pickup-point name. |
| `url` | string \| null | Deep link to the parcel's tracking page on anpost.com |
| `weight` | float \| null | Always `null` — An Post does not expose parcel weight. |
| `dimensions` | dict \| null | Always `null` — An Post does not expose parcel dimensions. |
| `history` | list \| null | Always `null` for now — the per-event timeline lives on a detail endpoint whose response shape has not yet been confirmed live. |
| `raw` | dict | The original An Post API payload |

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. An Post's own tracking-event category maps onto it as follows:

| `status` | Meaning | An Post category |
|---|---|---|
| `registered` | Item received into the network, or info received electronically ahead of the physical scan | `Item received`, `Info received` |
| `in_transit` | Moving through the network, including customs clearance | `In transit`, `Sorting`, `Customs` |
| `out_for_delivery` | *(never produced — An Post's own vocabulary has no same-day-delivery category)* | — |
| `at_pickup_point` | *(never produced — no "ready for collection" category, and no pickup-point field on the payload)* | — |
| `delivered` | Delivered | `Delivered` |
| `returning` | Going back to the sender | `Return to sender` |
| `problem` | An Post reports a delivery attempt or other exception | `Delivery attempted` |
| `unknown` | Not yet scanned (a watchlisted-but-unseen parcel), or a category we have not mapped yet | — |

An Post's own numeric category is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the An Post device):

| Event | When |
|---|---|
| `an_post_parcel_registered` | A new parcel appears in the active list |
| `an_post_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `an_post_parcel_delivered` | A parcel is delivered |
| `an_post_parcel_delivery_time_changed` | The expected delivery moment changes |

Every payload is the full normalised parcel plus the account's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `an_post.track_parcel` | `config_entry_id`, `tracking_code` | Add a bare tracking number to the chosen account's own watchlist |
| `an_post.untrack_parcel` | `config_entry_id`, `tracking_code` | Remove a tracking number from the watchlist |

These call An Post's own server-side watchlist, not local storage — the parcel is folded into the account's regular inbox by An Post itself, so it also appears if you open the *Track & Manage* app. `config_entry_id` picks which configured account to change when you have more than one.

## Examples

Ready-to-paste automations and a dashboard snippet live in [`examples/`](examples/), including adding a parcel to the watchlist from a Lovelace card.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.an_post: debug
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `invalid_auth` error during setup | Wrong e-mail or password |
| `cannot_connect` error during setup | An Post's identity service or `my-deliveries-api` is unreachable; check your network |
| A parcel shows `unknown` | An Post has not scanned it yet (typical right after adding it to the watchlist), or the tracking-event category is one we have not mapped |
| A status logs "Unrecognised An Post tracking-event category" | Please [open an issue](https://github.com/ha-parcel-integrations/ha-an-post/issues/new) with the logged line so the mapping can be extended |
| Reauth notification appears | The stored session expired or the password changed on An Post's side — sign in again on the notification |

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project with no affiliation, endorsement, or connection to An Post or any of its subsidiaries. The authentication chain and `my-deliveries-api` are undocumented and may change without notice. The maintainers have not asked An Post for permission to use this API; installing this integration may breach An Post's Terms of Service. You take any risk that follows — account suspension, service disruption, etc. No warranty (see [LICENSE](LICENSE)).

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
