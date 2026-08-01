# Security model

Deny by default. Every control below has a test.

## 1. Authentication

Two credentials, with different scopes.

**Device tokens** authenticate a phone for ingestion and for reading its own
status. They are `atl_` + 256 bits of `secrets` randomness, shown exactly
once at enrollment. Only `HMAC-SHA256(pepper, token)` is stored, and the
pepper lives in a `0600` file outside the database, so a copy of the
database alone cannot be used to test candidate tokens.

A slow KDF is deliberately not used: the token is 256 bits of randomness, so
there is nothing to brute force, and a per-request KDF would only add
latency to every upload.

Verification is constant-time (`hmac.compare_digest`), scoped to the device
in `X-Device-ID`, and returns an identical message for "unknown device" and
"wrong token" so an attacker cannot enumerate device ids.

**Admin** means either an ingress request (Supervisor authenticated the Home
Assistant user before proxying it) or a configured `admin_token`. The first
is only sound because the app publishes **no port** -- a test asserts
`ports` is absent from `config.yaml`, so that assumption cannot silently
lapse. If you do publish a port, set `admin_token` and turn
`trust_ingress_admin` off.

## 2. Authorisation

| Endpoint group | Who |
| --- | --- |
| `/api/v1/health` | anyone (no user data in the response) |
| `/api/v1/events/batch` | the authenticated device, for itself only |
| `/api/v1/devices/{id}/status` | that device only -- reading another's is 403 |
| everything else, including `/mcp` and `/` | admin |

A device cannot upload events whose `device_id` differs from its own, at the
batch level or per event.

## 3. Input handling

Enforced before anything is parsed or stored:

| Control | Limit |
| --- | --- |
| Request body | 4 MiB, checked before and after gzip decompression |
| Events per batch | 1000 |
| Single event | 64 KiB |
| Future-dated events | rejected beyond 24 h of clock skew |
| Unknown envelope fields | rejected (`extra="forbid"`) |
| Non-UTC timestamps | rejected; local time without an offset is never accepted |
| Unknown quality flags | rejected against a closed enum |
| Rate limit | 120 requests per device per minute, configurable |

Every SQL statement uses bound parameters; the only interpolation anywhere is
the number of `?` placeholders. A test stores `'; DROP TABLE raw_events; --`
as an ordinary payload value and then asserts the service still works.

## 4. Least privilege

From `config.yaml`, asserted by tests in `test-harness/mock-supervisor`:

- `homeassistant_api: true` -- the REST API proxy, used only to write entity
  states
- `hassio_api: false`, no `hassio_role` -- no Supervisor access at all
- no `map` -- no Home Assistant configuration, media or backup directory is
  mounted
- no `ports` -- ingress only
- no `host_network`, `host_pid`, `host_dbus`, `privileged`, `full_access`,
  `docker_api`, `devices`, `usb`, `uart`, `gpio`, `video`, `audio`
- `apparmor: true`

### Running as non-root

The Home Assistant base image runs s6-overlay as root, which is how apps are
expected to start under Supervisor. The service itself needs no root
privilege -- it writes only to `/data` and binds a high port. Dropping
privileges inside the container is not done today because it would fight
s6's supervision model; the CI job records the actual process user rather
than asserting a wish. This is a known gap, listed here rather than in a
claim.

## 5. Secret handling

| | |
| --- | --- |
| Device tokens | HMAC-SHA256 only; the plaintext exists solely in the enrollment response |
| Admin token | from an app option, never echoed by any endpoint |
| Supervisor token | from the environment, never logged, never in a response |
| Logging | no `Authorization` header, no token, no full event payload |
| Entities | aggregates only; no credential, no raw payload |
| MCP | no token surface at all |

Tests assert that no endpoint echoes the admin or device token, that the
container log contains neither, and that the cross-repository workflow's
logs contain neither.

## 6. MCP surface

Read-only by construction. `mcp_tools.py` contains no write path, no
`subprocess`, no file access. Ranges are capped at 31 days, pages at 1000
rows, and precise coordinates, message bodies and contact names are removed
with no parameter to re-enable them. Error messages never include a stack
trace, a path or a stored value.

## 7. Supply chain

- Runtime dependencies are pinned exactly in `android_timeline/requirements.txt`,
  which is what the container installs; a test asserts `pyproject.toml`
  agrees.
- `pip-audit --strict` runs against that exact file and against the dev
  toolchain.
- `bandit` runs over the app and the integration.
- CodeQL analyses `python` and `actions`.
- Trivy scans the built image; a fixable CRITICAL fails the build.
- The base image is pinned to an immutable date-suffixed tag.
- Dependabot covers Actions, both pip surfaces and Docker.
- Secret scanning with push protection is enabled, plus a CI job that greps
  full history.

## 8. CI hygiene

Top-level `permissions: contents: read`, widened per job only where needed.
`persist-credentials: false` on every checkout. Pull requests build the app
image but never publish it. No workflow has, or can obtain, access to a real
Home Assistant instance.

## 9. Known weaknesses

- **Ingress trust is an inference**, not a cryptographic check. It is sound
  only while no port is published.
- **Bearer tokens, not mutual TLS.** Rotation and revocation are the
  mitigation.
- **The pepper sits next to the database** it protects. It defends against a
  database copy, not against filesystem access.
- **The service runs as root inside the container** (see above).
- **Nothing here has ingested data from a real phone.** All of the above is
  verified against synthetic data in CI.
