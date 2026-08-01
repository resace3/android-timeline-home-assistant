# Installation

> Experimental. No physical phone has ever fed this server; see
> [testing.md](testing.md).

## 1. Add the repository

In Home Assistant:

**Settings -> Apps -> App store -> ⋮ (top right) -> Repositories**

Add:

```
https://github.com/resace3/android-timeline-home-assistant
```

Refresh the store. **Android Timeline** appears under a new section.

## 2. Install and start

Install it, then look at the **Configuration** tab before starting:

| Option | Leave as | Change if |
| --- | --- | --- |
| `timezone` | `UTC` | you want days to start at local midnight -- set an IANA name such as `Europe/London` |
| `features_enabled` | `true` | -- |
| `publish_entities` | `true` | you prefer the custom integration instead |
| `mcp_enabled` | `true` | you do not want MCP access |
| `trust_ingress_admin` | `true` | you have published a port (then also set `admin_token`) |
| `admin_token` | empty | you need API access from outside ingress |
| `retention_enabled` | `false` | you genuinely want raw events deleted -- this makes past analyses unreproducible |

Start the app. The **Open Web UI** button gives you a diagnostic page
through ingress.

## 3. Enroll your phone

Enrollment issues a token that is displayed **once** and stored only as a
keyed hash. There is no way to recover it -- only to rotate.

From a terminal on the Home Assistant host, or through the app's ingress URL:

```bash
curl -X POST http://<APP_HOST>:8099/api/v1/admin/devices \
  -H 'Authorization: Bearer <YOUR_ADMIN_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"device_id": "device-my-pixel-001", "display_name": "My phone"}'
```

The `admin_token` header is unnecessary if you are going through ingress.

Response:

```json
{
  "device_id": "device-my-pixel-001",
  "token_id": "tok_...",
  "token": "atl_...",
  "warning": "This token is shown once and is not recoverable. ..."
}
```

Choose a **pseudonymous** `device_id`. It ends up on every event forever, so
do not use a serial number, an IMEI or your name.

## 4. Configure the collector

On the phone, in Termux:

```bash
printf '%s' 'atl_PASTE_TOKEN_HERE' > ~/.config/android-timeline/token
chmod 600 ~/.config/android-timeline/token
```

Then set `server.base_url` in `~/.config/android-timeline/config.toml` to
your Home Assistant URL, and run:

```bash
android-timeline doctor --check-server
```

Full collector setup:
[android-timeline-termux/docs/installation.md](https://github.com/resace3/android-timeline-termux/blob/main/docs/installation.md).

### Reaching the app from the phone

The app is ingress-only by default, which means it is reachable at
`https://<your-home-assistant>/api/hassio_ingress/<token>/` -- a path that
rotates and is not suitable for a collector.

For a phone to upload you need a stable, authenticated HTTPS route. The
options, in order of preference:

1. **Home Assistant Cloud (Nabu Casa)** or your own reverse proxy in front
   of Home Assistant, with a path route to the app. Terminate TLS there.
2. **Publish a port** in the app configuration and put your own TLS
   terminator in front of it. If you do this, **set `admin_token` and turn
   `trust_ingress_admin` off** -- otherwise anything that can send an
   `X-Ingress-Path` header becomes an administrator.

Plaintext HTTP is refused by the collector outside an explicitly flagged
test mode, so a working setup needs real TLS.

## 5. Verify

```bash
curl -sf http://<APP_HOST>:8099/api/v1/health
```

Then, after the phone has uploaded at least once, open the app's Web UI: the
device should be listed with a last-seen time and an event count.

## 6. Entities

With `publish_entities: true` the app creates, per device:

```
sensor.android_timeline_<device>_last_sync
sensor.android_timeline_<device>_queue_status
sensor.android_timeline_<device>_events_today
sensor.android_timeline_<device>_data_coverage_today
sensor.android_timeline_<device>_battery_mean_today
sensor.android_timeline_<device>_charging_minutes_today
sensor.android_timeline_<device>_wifi_minutes_today
binary_sensor.android_timeline_<device>_collector_online
binary_sensor.android_timeline_<device>_data_complete_today
```

`data_complete_today` uses the `problem` device class: **on means there is a
problem** (the day has gaps).

## 7. Optional: the custom integration

If you prefer a config entry and a device registry entry:

1. Copy `custom_components/android_timeline/` into your Home Assistant
   `config/custom_components/`.
2. Restart Home Assistant.
3. **Settings -> Devices & services -> Add integration -> Android Timeline**
4. Enter the app URL and, if needed, the admin token.

You can run either mechanism or both.

## 8. MCP access

See [mcp.md](mcp.md) for Claude Desktop and Claude Code configuration.

## Upgrading

Home Assistant handles app updates. Database migrations run automatically at
start-up and are forward-only. Raw events are never touched by a migration.

## Uninstalling

Uninstalling the app removes its container. The `/data` volume, and so every
event, follows Home Assistant's normal app removal behaviour -- take a
backup first if you want to keep the data.
