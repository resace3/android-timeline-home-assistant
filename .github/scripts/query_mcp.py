"""Query the app's MCP server against the database a live container wrote.

The database is copied out of the container's volume first, so this proves
the MCP layer reads what ingestion actually persisted rather than anything
the test set up itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_failures: list[str] = []


def check(name: str, condition: bool, detail: Any = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    if not condition:
        _failures.append(name)


def copy_database(container: str, destination: Path) -> None:
    """Copy the app's /data out of the running container.

    ``docker cp`` from the live container rather than a volume mount, so the
    write-ahead log comes with it. Copying only the main database file would
    silently lose whatever is still in the WAL, which is most of what the
    test just wrote.
    """
    # Fixed argv, no shell. `docker` is on the runner PATH by definition:
    # this script only ever runs inside a GitHub Actions job.
    subprocess.run(  # noqa: S603
        ["docker", "cp", f"{container}:/data/.", str(destination)],
        check=True,
        capture_output=True,
    )
    print(f"copied: {sorted(p.name for p in destination.iterdir())}", flush=True)


def tool_json(result: Any, label: str) -> Any:
    """Parse a tool result, showing the raw content when it is not JSON."""
    if result.is_error:
        text = result.content[0].text if result.content else "<no content>"
        raise SystemExit(f"::error::{label} returned an error: {text[:400]}")
    if not result.content:
        raise SystemExit(f"::error::{label} returned no content")
    text = result.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"::error::{label} did not return JSON ({exc}): {text[:400]}"
        ) from exc


async def run(database_path: Path, device_id: str) -> None:
    from mcp import Client

    from android_timeline.app.config import Settings
    from android_timeline.app.database import Database
    from android_timeline.app.mcp_server import build_mcp_server

    settings = Settings(data_dir=database_path.parent, timezone="UTC")
    with Database(database_path) as database:
        server = build_mcp_server(database, settings)
        before = database.count_events(device_id)

        async with Client(server) as client:
            listing = await client.list_tools()
            names = {tool.name for tool in listing.tools}
            check("MCP initialises and lists tools", bool(names), len(names))
            check(
                "the documented tool set is present",
                {
                    "list_devices",
                    "get_day_timeline",
                    "find_data_gaps",
                    "get_data_coverage",
                    "query_phone_events",
                    "get_collector_status",
                }
                <= names,
            )

            devices = tool_json(await client.call_tool("list_devices", {}), "list_devices")
            check(
                "MCP sees the enrolled device",
                any(d["device_id"] == device_id for d in devices["devices"]),
            )

            timeline = tool_json(
                await client.call_tool(
                    "get_day_timeline",
                    {"device_id": device_id, "local_date": "2026-03-15"},
                ),
                "get_day_timeline",
            )
            check(
                "MCP returns 24 hour blocks",
                len(timeline["hour_blocks"]) == 24,
                len(timeline["hour_blocks"]),
            )
            check(
                "MCP reports the two-hour gap",
                timeline["missingness"]["hours_missing"] == 2,
                timeline["missingness"],
            )
            check("MCP states the timezone", timeline["timezone"] == "UTC")

            gaps = tool_json(
                await client.call_tool(
                    "find_data_gaps",
                    {
                        "device_id": device_id,
                        "start_utc": "2026-03-15T00:00:00Z",
                        "end_utc": "2026-03-16T00:00:00Z",
                    },
                ),
                "find_data_gaps",
            )
            check("MCP finds exactly one gap", gaps["gap_count"] == 1, gaps)

            location = tool_json(
                await client.call_tool(
                    "query_phone_events",
                    {
                        "device_id": device_id,
                        "start_utc": "2026-03-15T00:00:00Z",
                        "end_utc": "2026-03-16T00:00:00Z",
                        "sources": ["location"],
                        "limit": 5,
                    },
                ),
                "query_phone_events",
            )
            redacted = all(
                e["payload"]["latitude"] == "<redacted by the MCP server>"
                for e in location["events"]
            )
            check("MCP redacts coordinates", bool(location["events"]) and redacted)

            too_wide = await client.call_tool(
                "query_phone_events",
                {
                    "device_id": device_id,
                    "start_utc": "2020-01-01T00:00:00Z",
                    "end_utc": "2026-01-01T00:00:00Z",
                },
            )
            check("MCP bounds the date range", too_wide.is_error)

            mutation = await client.call_tool("delete_events", {"device_id": device_id})
            check("MCP exposes no mutation tool", mutation.is_error)

        check("MCP did not change the data", database.count_events(device_id) == before)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, help="running app container")
    parser.add_argument("--device", required=True)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        destination = Path(tmp)
        copy_database(args.container, destination)
        candidates = sorted(destination.glob("*.sqlite3"))
        if not candidates:
            print("::error::no database found in the container volume")
            return 1
        asyncio.run(run(candidates[0], args.device))

    print()
    if _failures:
        print(f"::error::{len(_failures)} MCP assertion(s) failed: {', '.join(_failures)}")
        return 1
    print("all MCP assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
