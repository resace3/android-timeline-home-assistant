# MCP server

A read-only Model Context Protocol server over the collected data.

## Guarantees

| | |
| --- | --- |
| Arbitrary SQL | **not available** |
| Shell execution | **not available** |
| Filesystem access | **not available** |
| Any mutation | **not available** |
| Phone control | **not available** |
| Date range | capped at 31 days per call |
| Page size | capped at 1000 rows, paginated with `offset`/`next_offset` |
| Redaction | unconditional; there is no parameter to disable it |

Redacted from every payload: `latitude`, `longitude`, `body`,
`contact_name`, `altitude_metres`. Coarse fields such as `place_category`,
`geohash` and salted pseudonyms survive, because they are what behavioural
analysis actually needs.

Tests enforce all of this: one scans every exposed tool name for
mutation-shaped fragments, one asserts no tool schema accepts an
"include sensitive" style parameter, and one calls a made-up mutating tool
and asserts the row count is unchanged.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_devices` | enrolled phones, with counts and last-seen times |
| `list_phone_sources` | which sources have produced data, and over what span |
| `get_phone_latest` | the most recent event for one source |
| `query_phone_events` | raw events in a bounded window, paginated |
| `get_day_timeline` | one day as hour blocks, features, coverage, gaps |
| `get_hourly_features` | stored hourly features for a window |
| `get_daily_features` | stored daily features between two ISO dates |
| `get_data_coverage` | hourly coverage plus a summary |
| `find_data_gaps` | contiguous windows with no data at all |
| `get_collector_status` | liveness: last heartbeat, queue depth, capabilities |
| `export_phone_window` | events, features and coverage for a bounded window |
| `describe_server` | versions, limits and the redaction policy |

## Transport

The app mounts a **Streamable HTTP** endpoint at `/mcp`, behind the same
admin check as the rest of the API. SSE is not offered: it was superseded in
protocol revision 2025-03-26.

A **stdio** entry point exists for local clients:

```bash
python -m app.mcp_server
```

## Client configuration

Placeholders only. Replace them with your own values; never commit a real
URL or token.

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "android-timeline": {
      "command": "docker",
      "args": [
        "exec", "-i", "addon_<REPOSITORY_HASH>_android_timeline",
        "python", "-m", "app.mcp_server"
      ]
    }
  }
}
```

### Claude Code

```bash
claude mcp add android-timeline \
  --transport http \
  "http://<YOUR_HOME_ASSISTANT_HOST>:<PORT>/mcp" \
  --header "Authorization: Bearer <YOUR_ADMIN_TOKEN>"
```

The HTTP transport requires either an ingress request or a configured
`admin_token`. Since the app publishes no port by default, reaching `/mcp`
from outside Home Assistant means deliberately exposing it -- at which point
set `admin_token` and turn `trust_ingress_admin` off.

## How to read the data

The server's own instructions say this to the model, and it matters:

- Raw events are immutable; derived features carry a `feature_version`.
- Every timestamp is UTC and ends in `Z`; each event also carries
  `timezone_offset_minutes`.
- **Absence is reported explicitly.** Call `get_data_coverage` or
  `find_data_gaps` before concluding anything from a flat line. A gap
  usually means the collector was not running, not that nothing happened.

## Error handling

Invalid arguments produce a plain sentence naming the constraint, for
example *"window is too wide: at most 31 days may be requested in one
call"*. Errors never contain a stack trace, a file path or a stored value; a
test asserts this.
