"""Android Timeline ingestion service, feature pipeline and MCP server."""

from __future__ import annotations

__all__ = [
    "FEATURE_VERSION",
    "PROTOCOL_VERSION",
    "SERVER_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "__version__",
]

__version__ = "0.1.0"

#: Reported in acknowledgements, health and diagnostics.
SERVER_VERSION = __version__

#: Wire protocol version shared with ``android-timeline-termux``.
PROTOCOL_VERSION = 1

#: Every event schema version this server accepts. The server must keep
#: accepting anything it has ever accepted: raw events are never rewritten,
#: so a phone that has been offline for months must still be able to sync.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

#: Version of the derived-feature definitions. Bumping this recomputes
#: features into new rows; it never touches raw events.
FEATURE_VERSION = 1
