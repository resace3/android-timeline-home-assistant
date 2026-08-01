"""Read-only MCP server.

Every tool is a thin wrapper over :mod:`mcp_tools`. There is deliberately
no tool that writes, deletes, runs a command, reads a file, executes SQL or
controls the phone -- the surface is queries over already-collected data,
and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server import MCPServer

from . import FEATURE_VERSION, SERVER_VERSION, mcp_tools
from .config import Settings
from .database import Database

__all__ = ["INSTRUCTIONS", "build_mcp_server"]

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read-only access to Android Timeline data collected by a Termux collector
on the user's own phone.

Important properties of this data:

* Raw events are immutable. Derived features carry a feature_version and
  can be recomputed; raw events never change.
* Every timestamp is UTC and ends in 'Z'. Each event also carries
  timezone_offset_minutes so local wall-clock time can be reconstructed.
* Absence of data is reported explicitly. Use get_data_coverage and
  find_data_gaps before drawing any conclusion from a flat line: a gap
  usually means the collector was not running, not that nothing happened.
* Precise coordinates, message bodies and contact names are removed by
  this server and cannot be requested through it.
* Date ranges are limited to 31 days and results are paginated.
"""


def build_mcp_server(database: Database, settings: Settings) -> MCPServer:
    """Construct the MCP server bound to a database and settings."""
    mcp = MCPServer(
        "android-timeline",
        instructions=INSTRUCTIONS,
    )

    @mcp.tool()
    def list_devices() -> dict[str, Any]:
        """List every enrolled phone, with event counts and last-seen times."""
        return mcp_tools.list_devices(database)

    @mcp.tool()
    def list_phone_sources(device_id: str | None = None) -> dict[str, Any]:
        """List the data sources that have produced events, and their spans."""
        return mcp_tools.list_phone_sources(database, device_id)

    @mcp.tool()
    def get_phone_latest(device_id: str, source: str) -> dict[str, Any]:
        """Get the most recent event for one source on one device."""
        return mcp_tools.get_phone_latest(database, device_id, source)

    @mcp.tool()
    def query_phone_events(
        device_id: str,
        start_utc: str,
        end_utc: str,
        sources: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Query raw events in a UTC window (at most 31 days), paginated."""
        return mcp_tools.query_phone_events(
            database, device_id, start_utc, end_utc, sources, limit, offset
        )

    @mcp.tool()
    def get_day_timeline(device_id: str, local_date: str | None = None) -> dict[str, Any]:
        """Get one day as hour blocks with features, coverage and gaps.

        Defaults to yesterday in the server's configured timezone.
        """
        return mcp_tools.get_day_timeline(database, settings, device_id, local_date)

    @mcp.tool()
    def get_hourly_features(
        device_id: str,
        start_utc: str,
        end_utc: str,
        feature_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get stored hourly derived features for a UTC window."""
        return mcp_tools.get_hourly_features(
            database, device_id, start_utc, end_utc, feature_names
        )

    @mcp.tool()
    def get_daily_features(
        device_id: str,
        start_date: str,
        end_date: str,
        feature_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get stored daily derived features between two ISO dates."""
        return mcp_tools.get_daily_features(
            database, device_id, start_date, end_date, feature_names
        )

    @mcp.tool()
    def get_data_coverage(device_id: str, start_utc: str, end_utc: str) -> dict[str, Any]:
        """Get hourly data coverage, so missing periods are explicit."""
        return mcp_tools.get_data_coverage(database, device_id, start_utc, end_utc)

    @mcp.tool()
    def find_data_gaps(
        device_id: str, start_utc: str, end_utc: str, min_hours: int = 1
    ) -> dict[str, Any]:
        """Find contiguous windows in which no data was collected at all."""
        return mcp_tools.find_data_gaps(database, device_id, start_utc, end_utc, min_hours)

    @mcp.tool()
    def get_collector_status(device_id: str) -> dict[str, Any]:
        """Get collector liveness: last heartbeat, queue depth, capabilities."""
        return mcp_tools.get_collector_status(database, device_id)

    @mcp.tool()
    def export_phone_window(
        device_id: str,
        start_utc: str,
        end_utc: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Export events, hourly features and coverage for a bounded window."""
        return mcp_tools.export_phone_window(
            database, device_id, start_utc, end_utc, limit, offset
        )

    @mcp.tool()
    def describe_server() -> dict[str, Any]:
        """Describe versions, limits and the redaction policy of this server."""
        return {
            "server_version": SERVER_VERSION,
            "feature_version": FEATURE_VERSION,
            "read_only": True,
            "max_range_days": mcp_tools.MAX_RANGE_DAYS,
            "max_page_size": mcp_tools.MAX_PAGE_SIZE,
            "timezone": settings.timezone,
            "redacted_fields": sorted(mcp_tools.REDACTED_KEYS),
            "capabilities_absent": [
                "no arbitrary SQL",
                "no shell execution",
                "no filesystem access",
                "no mutation of any kind",
                "no phone control",
            ],
        }

    return mcp


def main() -> None:  # pragma: no cover - stdio entry point
    """Run the MCP server over stdio, for Claude Desktop and Claude Code."""
    from .config import load_settings

    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(message)s")
    database = Database(settings.database_path)
    build_mcp_server(database, settings).run()


if __name__ == "__main__":  # pragma: no cover
    main()
