"""The MCP server, exercised through the real protocol.

``Client(server)`` connects in memory: no subprocess, no port, but the call
still goes through listing, validation and invocation exactly as it would
over Streamable HTTP.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp import Client

from android_timeline.app.config import Settings
from android_timeline.app.database import Database
from android_timeline.app.ingestion import ingest_batch
from android_timeline.app.mcp_server import build_mcp_server
from android_timeline.app.models import EventBatch

pytestmark = pytest.mark.integration

DEVICE = "device-test-001"
DAY_START = datetime(2026, 3, 15, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)

EXPECTED_TOOLS = {
    "list_phone_sources",
    "list_devices",
    "get_phone_latest",
    "query_phone_events",
    "get_day_timeline",
    "get_hourly_features",
    "get_daily_features",
    "get_data_coverage",
    "find_data_gaps",
    "get_collector_status",
    "export_phone_window",
    "describe_server",
}

#: Anything matching these must never appear as a tool: the server is
#: read-only and has no side effects at all.
FORBIDDEN_TOOL_FRAGMENTS = (
    "sql",
    "query_raw",
    "exec",
    "shell",
    "command",
    "run_",
    "write",
    "delete",
    "update",
    "insert",
    "create",
    "set_",
    "read_file",
    "file",
    "path",
    "send",
    "control",
)


@pytest.fixture
def loaded_server(
    database: Database, settings: Settings, synthetic_day: dict[str, Any]
) -> Any:
    database.upsert_device(DEVICE)
    events = synthetic_day["events"]
    for start in range(0, len(events), 200):
        ingest_batch(
            database,
            settings,
            EventBatch.model_validate(
                {
                    "protocol_version": 1,
                    "batch_id": f"batch-test-{start:04d}",
                    "device_id": DEVICE,
                    "collector_version": "0.1.0",
                    "created_time_utc": "2026-03-15T00:00:00Z",
                    "events": events[start : start + 200],
                }
            ),
        )
    return build_mcp_server(database, settings)


def payload(result: Any) -> Any:
    """Extract the JSON body from a CallToolResult."""
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


class TestProtocol:
    async def test_initialises_and_lists_tools(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            listing = await client.list_tools()
        names = {tool.name for tool in listing.tools}
        assert names >= EXPECTED_TOOLS

    async def test_every_tool_has_a_description_and_schema(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            listing = await client.list_tools()
        for tool in listing.tools:
            assert tool.description, tool.name
            assert tool.input_schema["type"] == "object", tool.name

    async def test_server_reports_instructions(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            assert client.instructions
            assert "read-only" in client.instructions.lower()


class TestReadOnly:
    async def test_no_mutating_or_executing_tool_is_exposed(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}

        for name in names:
            for fragment in FORBIDDEN_TOOL_FRAGMENTS:
                assert fragment not in name, f"{name} looks like a {fragment} tool"

    async def test_calling_an_unknown_tool_fails(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            result = await client.call_tool("execute_sql", {"sql": "DROP TABLE raw_events"})
        assert result.is_error

    async def test_data_is_unchanged_after_a_full_tool_sweep(
        self, loaded_server: Any, database: Database
    ) -> None:
        before = database.count_events(DEVICE)
        async with Client(loaded_server) as client:
            await client.call_tool("list_devices", {})
            await client.call_tool(
                "get_day_timeline", {"device_id": DEVICE, "local_date": "2026-03-15"}
            )
            await client.call_tool(
                "export_phone_window",
                {
                    "device_id": DEVICE,
                    "start_utc": "2026-03-15T00:00:00Z",
                    "end_utc": "2026-03-16T00:00:00Z",
                },
            )
        assert database.count_events(DEVICE) == before


class TestQueries:
    async def test_list_devices(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            body = payload(await client.call_tool("list_devices", {}))
        assert body["count"] == 1
        assert body["devices"][0]["device_id"] == DEVICE

    async def test_list_phone_sources(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            body = payload(await client.call_tool("list_phone_sources", {"device_id": DEVICE}))
        sources = {s["source"] for s in body["sources"]}
        assert {"battery", "wifi", "location", "heartbeat"} <= sources

    async def test_get_phone_latest(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            body = payload(
                await client.call_tool(
                    "get_phone_latest", {"device_id": DEVICE, "source": "battery"}
                )
            )
        assert body["event"]["source"] == "battery"

    async def test_get_day_timeline_returns_24_blocks(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            body = payload(
                await client.call_tool(
                    "get_day_timeline",
                    {"device_id": DEVICE, "local_date": "2026-03-15"},
                )
            )
        assert len(body["hour_blocks"]) == 24
        assert body["timezone"] == "UTC"
        assert body["missingness"]["hours_missing"] == 2

    async def test_find_data_gaps_finds_the_synthetic_gap(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            body = payload(
                await client.call_tool(
                    "find_data_gaps",
                    {
                        "device_id": DEVICE,
                        "start_utc": "2026-03-15T00:00:00Z",
                        "end_utc": "2026-03-16T00:00:00Z",
                    },
                )
            )
        assert body["gap_count"] == 1
        assert body["gaps"][0]["hours"] == 2

    async def test_get_collector_status(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            body = payload(
                await client.call_tool("get_collector_status", {"device_id": DEVICE})
            )
        assert "battery" in body["enabled_collectors"]


class TestBoundsAndValidation:
    async def test_window_wider_than_31_days_is_refused(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            result = await client.call_tool(
                "query_phone_events",
                {
                    "device_id": DEVICE,
                    "start_utc": "2020-01-01T00:00:00Z",
                    "end_utc": "2026-01-01T00:00:00Z",
                },
            )
        assert result.is_error
        assert "31 days" in result.content[0].text

    async def test_limit_above_the_maximum_is_refused(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            result = await client.call_tool(
                "query_phone_events",
                {
                    "device_id": DEVICE,
                    "start_utc": "2026-03-15T00:00:00Z",
                    "end_utc": "2026-03-16T00:00:00Z",
                    "limit": 100000,
                },
            )
        assert result.is_error

    async def test_unknown_device_gives_a_helpful_error(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            result = await client.call_tool(
                "get_collector_status", {"device_id": "device-test-999"}
            )
        assert result.is_error
        assert "unknown device_id" in result.content[0].text

    async def test_a_malformed_timestamp_is_refused(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            result = await client.call_tool(
                "query_phone_events",
                {
                    "device_id": DEVICE,
                    "start_utc": "yesterday please",
                    "end_utc": "2026-03-16T00:00:00Z",
                },
            )
        assert result.is_error

    async def test_errors_never_leak_a_stack_trace_or_path(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            result = await client.call_tool(
                "get_collector_status", {"device_id": "device-test-999"}
            )
        text = result.content[0].text
        assert "Traceback" not in text
        assert "/data" not in text
        assert ".py" not in text

    async def test_pagination_is_consistent(self, loaded_server: Any) -> None:
        args = {
            "device_id": DEVICE,
            "start_utc": "2026-03-15T00:00:00Z",
            "end_utc": "2026-03-16T00:00:00Z",
            "limit": 10,
        }
        async with Client(loaded_server) as client:
            first = payload(await client.call_tool("query_phone_events", args))
            second = payload(
                await client.call_tool(
                    "query_phone_events", {**args, "offset": first["next_offset"]}
                )
            )

        assert first["has_more"] is True
        assert len(first["events"]) == 10
        first_ids = {e["event_id"] for e in first["events"]}
        second_ids = {e["event_id"] for e in second["events"]}
        assert not (first_ids & second_ids)


class TestRedaction:
    async def test_coordinates_are_removed(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            body = payload(
                await client.call_tool(
                    "query_phone_events",
                    {
                        "device_id": DEVICE,
                        "start_utc": "2026-03-15T00:00:00Z",
                        "end_utc": "2026-03-16T00:00:00Z",
                        "sources": ["location"],
                        "limit": 5,
                    },
                )
            )
        assert body["events"]
        for event in body["events"]:
            assert event["payload"]["latitude"] == "<redacted by the MCP server>"
            assert event["payload"]["longitude"] == "<redacted by the MCP server>"
            # Coarse, non-identifying fields survive.
            assert event["payload"]["place_category"].startswith("place-")

    async def test_no_tool_can_request_unredacted_data(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            listing = await client.list_tools()
        for tool in listing.tools:
            properties = tool.input_schema.get("properties", {})
            assert "include_sensitive" not in properties
            assert "redact" not in properties

    async def test_describe_server_states_the_policy(self, loaded_server: Any) -> None:
        async with Client(loaded_server) as client:
            body = payload(await client.call_tool("describe_server", {}))
        assert body["read_only"] is True
        assert "latitude" in body["redacted_fields"]
        assert body["max_range_days"] == 31
