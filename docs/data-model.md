# Data model

## Principle

Raw events are the ground truth and are never rewritten. Everything else is
derived, versioned and reproducible from them.

## Tables

| Table | Contents |
| --- | --- |
| `devices` | one row per enrolled phone: pseudonymous id, display name, enabled flag, last seen |
| `device_tokens` | keyed hashes only, with created/last-used/revoked timestamps |
| `raw_events` | the event store, `event_id` primary key, append-only |
| `received_batches` | one row per upload, with per-batch counts and a replay counter |
| `batch_event_acknowledgements` | the verdict issued for every event in every batch |
| `feature_definitions` | name, version, description, unit, source event types |
| `hourly_features` | one row per (device, feature, version, hour) |
| `daily_features` | one row per (device, feature, version, local date) |
| `collector_heartbeats` | liveness projection of heartbeat events |
| `data_coverage` | one row per hour, **including empty hours** |
| `interventions` | user-annotated periods, for later causal work |
| `schema_migrations` | applied migration versions |

## The event envelope

```json
{
  "event_id": "6f1a7a1e-...",
  "device_id": "device-test-001",
  "source": "battery",
  "event_type": "battery_sample",
  "event_time_utc": "2026-07-31T23:45:00Z",
  "collected_time_utc": "2026-07-31T23:45:03Z",
  "timezone_offset_minutes": -240,
  "schema_version": 1,
  "quality_flags": [],
  "payload": {}
}
```

`event_time_utc` is when it happened; `collected_time_utc` is when the phone
observed it. They differ for backfilled and late-arriving data, and
collapsing them would destroy the ability to detect lag.
`timezone_offset_minutes` is retained so wall-clock time can be
reconstructed after travel or a daylight-saving change.

`event_id` is normally a UUIDv5 over
`(device_id, source, event_type, event_time_utc, payload)`, which is what
makes deduplication identical on both sides.

## Idempotency

Three replay paths, all no-ops, all tested:

| Path | Behaviour |
| --- | --- |
| The same event twice inside one batch | collapsed before insert |
| The same batch id sent again | every event reported `duplicate`; `replay_count` increments |
| The same events under a new batch id | every event reported `duplicate` |

`accepted` contains both `stored` and `duplicate`. The collector must treat
them identically -- that equivalence is the whole basis of safe retries.

## Features

Every derived row carries: feature name, feature version, window start and
end, the source event types it was computed from, the value, the coverage
proportion of the window, quality flags, and a computation timestamp.

| Feature | Unit | Daily aggregation |
| --- | --- | --- |
| `battery_percentage_mean` | percent | mean |
| `charging_minutes` | minutes | sum |
| `wifi_connected_minutes` | minutes | sum |
| `location_observation_coverage` | proportion | mean |
| `movement_summary` | m/s² | mean |
| `calls_count` | count | sum |
| `sms_count` | count | sum |
| `collector_heartbeat_coverage` | proportion | mean |
| `events_total` | count | sum |
| `missing_data_flag` | boolean | sum (hours missing) |

`charging_minutes` and `wifi_connected_minutes` are **inferred from
periodic samples**: each positive sample stands for its share of the hour, so
a state change between samples is invisible. This is why every value ships
with a coverage proportion.

A feature value of `None` means "not measured". Zero means "measured, and it
was zero". They are not the same and are stored differently.

## Coverage

For each hour: the sources expected (from the device's latest heartbeat, not
from server-side configuration -- only the phone knows what the user
enabled), the sources observed, the event count, the heartbeat count, the
proportion, and `is_missing`.

**Every hour gets a row, including empty ones.** An absent row would be
indistinguishable from an unobserved one, which is exactly the confusion this
project exists to prevent.

## Backward compatibility

- `protocol_version` changes only for a breaking wire change; both
  repositories must be released together.
- `schema_version` is stored per event and never rewritten. The server must
  keep accepting every version it has ever accepted -- a phone offline for
  months must still be able to sync.
- Adding an optional field to a `payload` is not breaking.
- Adding a `quality_flags` value is schema-visible (the enum is closed) and
  requires a `schema_version` bump.
- Renaming a `source` or `event_type` is breaking and effectively never
  acceptable: it orphans historical data.
- `FEATURE_VERSION` is independent. Bumping it recomputes features into new
  rows and never touches raw events.

## Retention

Off by default. When enabled, raw events older than the window are deleted --
which makes past analyses unreproducible, so the option is documented as a
trade-off rather than a tidy-up.
