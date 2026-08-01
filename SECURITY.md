# Security policy

## Status

Experimental personal software. No physical Android device has ever fed
this server. Read [docs/security.md](docs/security.md) and
[docs/testing.md](docs/testing.md) before deploying it anywhere that matters.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:
**Security -> Report a vulnerability** on
<https://github.com/resace3/android-timeline-home-assistant>.

Do not open a public issue, and do not include a real Home Assistant URL, a
real token or real personal data -- a synthetic reproduction is always
enough.

Best-effort response; there is no service-level commitment.

## Supported versions

Only the latest tagged release and `main`.

## Design decisions with security consequences

| Decision | Consequence | Mitigation |
| --- | --- | --- |
| Ingress requests are treated as admin | Anyone who can reach the app *as if via ingress* is an admin | No port is published, so only Supervisor can produce such a request. Publishing a port means setting `admin_token` and disabling `trust_ingress_admin`. |
| Device tokens are bearer tokens | A leaked token allows writing events for that device | Rotation and revocation take effect immediately; tokens are scoped to one device and cannot read another's data |
| Tokens are hashed with HMAC-SHA256, not a slow KDF | A leaked database plus a leaked pepper would allow offline verification | Tokens are 256 bits of `secrets` randomness, so there is nothing to guess; the pepper lives outside the database |
| SQLite in `/data` | Anyone with the app's volume has the data | Home Assistant's own backup and access model applies |
| The app trusts what the phone sends | A compromised phone can inject false events | Events are attributable to a device and immutable; a bad device can be disabled and its tokens revoked |

## What this app never does

Enforced by tests, not just policy:

- Accept unauthenticated ingestion.
- Expose the database, arbitrary SQL, or a shell -- through the API or MCP.
- Store a plaintext token.
- Accept a token in a query parameter.
- Log an `Authorization` header, a token, or a full event payload.
- Create a Home Assistant entity per raw event.
- Request a Supervisor role, host networking, a device, or a Home Assistant
  directory mount.
- Delete a raw event unless retention is explicitly enabled.
- Return precise coordinates, message bodies or contact names through MCP.

## Reporting scope

In scope: authentication bypass, injection, privilege escalation through
the Supervisor API, data leakage through MCP or entities, and anything that
lets one enrolled device read or write another's data.

Out of scope: attacks requiring root on the Home Assistant host, a
malicious Home Assistant instance (the user runs both sides), and denial of
service against a personal deployment that is not internet-facing.
