# Architecture

## Module map

| Module | Responsibility |
| --- | --- |
| `config.py` | app options from `/data/options.json` plus environment overrides |
| `models.py` | Pydantic wire models mirroring `schemas/` |
| `database.py` | SQLite: migrations, event store, features, coverage |
| `auth.py` | token issuance, verification, rotation, revocation, rate limiting |
| `ingestion.py` | batch validation and storage |
| `acknowledgements.py` | building and replaying per-event verdicts |
| `features.py` | versioned hourly and daily feature pipeline |
| `coverage.py` | hourly coverage and gap detection |
| `timeline.py` | day assembly, timezone resolution |
| `mcp_tools.py` | read-only query functions (no write path exists here) |
| `mcp_server.py` | MCP wiring over those functions |
| `home_assistant.py` | summary entity publication via the Supervisor proxy |
| `main.py` | FastAPI app, routing, auth dependencies, maintenance loop |

Every import inside `app/` is relative, because the package is
`android_timeline.app` in tests and plain `app` inside the container.

## Request path

```
POST /api/v1/events/batch
  -> body size check (before parsing)
  -> gzip decompression, size checked again
  -> JSON parse
  -> Pydantic EventBatch (extra="forbid", closed enums, UTC-only timestamps)
  -> device token verification, scoped to X-Device-ID
  -> rate limit
  -> per-event validation (size, clock skew, device match)
  -> INSERT OR IGNORE on event_id
  -> heartbeat projection
  -> batch row + per-event acknowledgement rows
  -> Acknowledgement response
```

Nothing after the token check can cause a partially-acknowledged batch to be
reported as fully accepted: verdicts are derived from what the database
actually did, not from what was attempted.

## Why acknowledgement is per event

A batch-level "OK" forces the collector into a bad choice when only some
events land: re-send everything (creating duplicates unless the server is
idempotent anyway) or drop the difference (losing data). Per-event
acknowledgement lets the phone mark exactly what the server holds and keep
the rest queued. `stored` and `duplicate` are both success, and that
equivalence is what makes a retry safe.

## Why coverage is a table, not a computation

An hour with no events could mean the user was asleep, or that the collector
was killed by the phone's battery optimiser. Those are different facts and
downstream analysis must not confuse them. Writing a row for **every** hour,
including empty ones, makes "we were not watching" representable. An absent
row would not be.

Expected sources come from the device's most recent heartbeat rather than
from server-side configuration, because only the phone knows what the user
actually enabled.

## Why features are versioned separately from events

Feature definitions will change; the observations will not. Storing
`feature_version` on every derived row means an old analysis stays
interpretable after a definition changes, and recomputation writes new rows
rather than mutating old ones. Raw events are never touched by the feature
pipeline -- a test asserts the event count is unchanged after a recompute.

## The maintenance loop

An in-process `asyncio` task, every five minutes: recompute recent hourly
features, roll up yesterday, publish entities, apply retention if enabled.
It catches everything except cancellation, because a failed maintenance
cycle must never take down ingestion -- ingestion is the part that cannot be
retried later.

For one phone this is comfortably enough. For several it should become a
scheduled job; that is an open question in
[research-and-decisions.md](research-and-decisions.md).

## MCP inside the same app

The MCP server is mounted at `/mcp` as a Streamable HTTP ASGI app. Two
details of the v2 SDK shape this:

- A mounted app's own lifespan never runs, so `create_app` enters
  `mcp.session_manager.run()` in the FastAPI lifespan. Without it the first
  `/mcp` request fails with *"Task group is not initialized"*.
- The transport's DNS-rebinding guard defaults to a localhost-only
  allowlist. Behind ingress the `Host` header is Home Assistant's, so the
  guard is disabled explicitly -- the upstream documentation calls this the
  honest configuration when a reverse proxy already controls that header.

Access is gated by the same admin check as the rest of the API, applied in
middleware because the mounted app has its own routing.

## Home Assistant integration, two ways

The app can publish entity states itself through the Supervisor's Home
Assistant API proxy -- no user setup, no config entry. The optional custom
integration in `custom_components/` adds a config entry, a device registry
entry and proper unavailability handling for people who prefer them, and
reads the app's HTTP API rather than its database so the two can run on
different hosts.

Only aggregates are published either way. One entity per raw event would
fill the recorder database with behavioural detail that belongs in MCP.

## Storage choice

SQLite in the app's `/data` volume. A personal deployment produces on the
order of 10^4 events a day, which SQLite handles comfortably while needing
no second container, no credentials and no backup story beyond Home
Assistant's own. Migrations execute statement by statement rather than
through `executescript`, which implicitly commits and would break the
surrounding transaction -- the same bug the collector had, fixed the same
way.
