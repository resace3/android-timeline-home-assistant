# Research and decisions

Findings from surveying current upstream documentation before writing code,
and the decisions that followed. Dated **July 2026**; re-check before
assuming any of it is still true. The phone side is documented in
[`android-timeline-termux/docs/research-and-decisions.md`](https://github.com/resace3/android-timeline-termux/blob/main/docs/research-and-decisions.md).

---

## 1. Home Assistant add-ons are now "apps"

**Found.** Home Assistant 2026.2 renamed *add-ons* to *apps* across the UI,
the store and the documentation. Nothing about how they work changed: they
are still Docker containers managed by Supervisor, still described by
`config.yaml`, still discovered through a `repository.yaml` at the root of a
git repository.

**Decision.** Use "app" in all user-facing text and in the OCI label
(`io.hass.type=app`, which is what the current official example uses), while
keeping the directory layout the Supervisor expects.

## 2. The legacy builder is retired

**Found.** `home-assistant/builder@master` now prints a deprecation warning
and is no longer maintained. The
[migration note](https://developers.home-assistant.io/blog/2026/04/02/builder-migration/)
replaces it with focused composite actions that delegate to the runner's own
Docker BuildKit:

- `home-assistant/builder/actions/prepare-multi-arch-matrix`
- `home-assistant/builder/actions/build-image`
- `home-assistant/builder/actions/publish-multi-arch-manifest`

`build.yaml` is gone: `build_from` becomes `FROM`, `labels` become `LABEL`,
`args` become `ARG`. The matrix maps `amd64` to `ubuntu-24.04` and `aarch64`
to `ubuntu-24.04-arm`, so aarch64 builds natively rather than under QEMU.
The official example repository is `home-assistant/apps-example`.

**Decision.** Use the new actions pinned to `2026.06.0`. No `build.yaml`.
The Dockerfile declares `ARG BUILD_FROM` and fails fast if `TARGETARCH` is
unset, which is the documented signal that BuildKit is not being used.
Pull requests build without pushing; only a release publishes and signs.

## 3. Base image

**Found.** `ghcr.io/home-assistant/base-python` provides a verified CPython
on Alpine, with immutable date-suffixed tags alongside the moving ones.

**Decision.** `ghcr.io/home-assistant/base-python:3.13-alpine3.23-2026.06.1`
-- an immutable tag, so a rebuild of an old commit produces the same base.
Dependabot moves it forward.

## 4. Ingress, ports and what "admin" means

**Found.** Supervisor authenticates the Home Assistant user before proxying
an ingress request and adds `X-Ingress-Path`. An app that publishes no port
can only be reached through that proxy.

**Decision.** Ingress only; no `ports` in `config.yaml`. An ingress request
is therefore treated as an authenticated administrator, which avoids
inventing a second login for a single-user personal app. Because that
inference is only sound while no port is published, `trust_ingress_admin` is
a documented option and an `admin_token` alternative exists. A test asserts
`ports` is absent from `config.yaml`, so the assumption cannot silently
stop being true.

## 5. Supervisor privileges

**Found.** `hassio_api` plus `hassio_role` grants access to the Supervisor
itself; `homeassistant_api` grants only the Home Assistant REST API proxy at
`http://supervisor/core/api`.

**Decision.** `homeassistant_api: true`, `hassio_api: false`, no
`hassio_role`, no `map`, no `devices`, no `host_network`. Publishing a
handful of entity states is the only thing this app needs from Home
Assistant. Four tests in `test-harness/mock-supervisor` assert that none of
the wider privileges creep back in.

## 6. MCP SDK v2

**Found.** The MCP Python SDK is now at v2, with a changed API:
`from mcp.server import MCPServer`, `@mcp.tool()`, `mcp.streamable_http_app()`
for ASGI, and `Client(server)` for an in-memory client. SSE was superseded by
Streamable HTTP in protocol revision 2025-03-26 and exists only for old
clients.

Two v2 details matter here:

- **A mounted MCP app's own lifespan never runs.** The host application must
  enter `mcp.session_manager.run()` itself, or the first request to `/mcp`
  fails with *"Task group is not initialized"*.
- The transport arms DNS-rebinding protection with a localhost-only
  allowlist by default. Behind ingress the `Host` header is Home
  Assistant's, so every request would return `421 Misdirected Request`.

**Decision.** Streamable HTTP mounted into the FastAPI app, with
`session_manager.run()` entered in the FastAPI lifespan, and
`TransportSecuritySettings(enable_dns_rebinding_protection=False)` -- which
the upstream documentation calls the honest configuration behind a reverse
proxy that already controls the `Host` header. A stdio entry point
(`python -m app.mcp_server`) is provided for Claude Desktop and Claude Code.
Tests use the in-memory `Client`, which exercises the real protocol layer.

## 7. Read-only by construction

**Decision.** The MCP tool functions live in `mcp_tools.py`, which contains
no write path at all: no `INSERT`, no `UPDATE`, no `DELETE`, no `subprocess`,
no file access. `mcp_server.py` is a thin wrapper. Two tests enforce this
from the outside -- one scans every exposed tool name for mutation-shaped
fragments, the other calls a made-up mutating tool and asserts the row count
is unchanged.

Redaction is unconditional. There is no `include_sensitive` parameter,
and a test asserts no tool schema has one.

## 8. Storage

**Found.** A personal deployment produces on the order of 10^4 events per
day. SQLite handles that comfortably and needs no second container, no
credentials and no separate backup story.

**Decision.** SQLite in the app's `/data` volume, with explicit migrations
and no ORM. As in the collector, migrations execute statement by statement
rather than through `executescript`, which implicitly commits and would
break the surrounding transaction. Postgres is on the roadmap only if a
real deployment outgrows this.

## 9. Entities

**Found.** Home Assistant's recorder stores every state change. One entity
per raw event would make the recorder database enormous and the UI useless.

**Decision.** Seven sensors and two binary sensors per device, all
aggregates. Detail belongs to MCP and the API. A harness test asserts the
entity count stays small even when the database holds hundreds of events.

## 10. Custom integration vs. app-published entities

**Decision.** Both, because they serve different situations. The app can
push states through the Supervisor proxy with no user setup at all; the
optional custom integration adds a config entry, a device registry entry and
proper unavailability handling for people who want them. The integration is
validated by `hassfest` and loaded in a real Home Assistant Core container
(non-blocking), and is kept separately testable.

## 11. Protocol source of truth

**Decision.** This repository owns `schemas/`. The collector vendors a
byte-identical copy guarded by a checksum file, and the cross-repository
workflow diffs the two directly, so a checksum updated on only one side is
still caught. Rationale: the server is the party that must keep accepting
old data forever, so it should own the contract.

## 12. What is deliberately not tested in CI

**Found.** Running Supervisor in GitHub Actions requires privileged
Docker-in-Docker or a full Home Assistant OS image. Both are slow and
flaky.

**Decision.** Do not attempt it. Layer B mocks the single Supervisor
endpoint this app calls and contract-tests it; Layer C runs real Home
Assistant Core for the integration; Layer D proves the image builds with the
official actions. `docs/testing.md` states exactly what that leaves
unvalidated rather than implying full coverage.

---

## Open questions

- Should feature computation move to a scheduled job rather than the
  in-process maintenance loop? Fine for one phone; not for several.
- Is `charging_minutes` from periodic samples good enough, or should the
  collector emit charging *transitions* instead?
- Would signed acknowledgements be worth it, so a phone can verify the
  server it uploaded to?
