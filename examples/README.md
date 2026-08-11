# Examples

Ready-to-paste Home Assistant snippets for the An Post integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |
| [`dashboards/`](dashboards/) | A helper + script + Lovelace card for adding a bare tracking number to your account's watchlist from a dashboard. |

Parcels the account already knows about show up automatically — nothing to
register by hand for those. A parcel An Post has not yet linked to your
account (a gift, a parcel sent to you by someone else) needs one extra step:
add its tracking number to the account's watchlist via the integration's
`an_post.track_parcel` service (see [`dashboards/`](dashboards/)) — An Post
then folds it into the regular inbox automatically.

All examples assume a single An Post account. Adjust entity IDs to match
yours; with more than one account configured, every entity ID carries the
account name, and the services need a `config_entry_id` to pick which one.

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `an_post_parcel_registered` | A new parcel appears in the active list | The full normalised parcel dict |
| `an_post_parcel_status_changed` | A parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `an_post_parcel_delivered` | A parcel reaches the delivered status | Same (fires *instead of* `status_changed` on that final hop) |
| `an_post_parcel_delivery_time_changed` | A parcel's expected delivery time changes | Same, plus `old_planned_from` / `new_planned_from` / `old_planned_to` / `new_planned_to` |

Every payload also carries the account's `device_id`, which is what device
triggers filter on. Events are suppressed on the first refresh after start-up.
