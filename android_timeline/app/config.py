"""Runtime configuration.

Home Assistant writes the user's app options to ``/data/options.json``.
Environment variables override them, which is how the test harness and CI
configure the service without inventing a Supervisor.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Settings", "load_settings"]

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    """Everything the service needs to start."""

    #: Persistent directory. Home Assistant mounts /data per app.
    data_dir: Path = field(default_factory=lambda: Path("/data"))
    log_level: str = "INFO"

    #: Ingestion limits. Enforced before parsing, not after.
    max_batch_bytes: int = 4 * 1024 * 1024
    max_batch_events: int = 1000
    max_event_bytes: int = 64 * 1024

    #: Reject events dated further ahead than this (clock skew / spoofing).
    max_future_skew_seconds: int = 24 * 3600
    #: Events older than this are stored but flagged as late arrivals.
    late_arrival_seconds: int = 6 * 3600

    #: Per-device request budget. Crude but enough to stop a runaway client.
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60

    #: Admin surface. Ingress requests are authenticated by Supervisor
    #: before they reach us, so a valid ingress request is treated as an
    #: admin. Setting an admin token additionally allows direct access.
    trust_ingress_admin: bool = True
    admin_token: str = ""

    #: Feature pipeline.
    features_enabled: bool = True
    feature_recompute_hours: int = 48
    #: IANA timezone used to bucket days. "UTC" keeps CI deterministic.
    timezone: str = "UTC"

    #: Home Assistant integration.
    publish_entities: bool = True
    supervisor_token: str = ""
    supervisor_url: str = "http://supervisor"

    #: MCP.
    mcp_enabled: bool = True
    mcp_path: str = "/mcp"
    #: Behind ingress the Host header is Home Assistant's, not localhost,
    #: so the transport's DNS-rebinding guard must be told to stand down.
    mcp_allowed_hosts: list[str] = field(default_factory=lambda: ["*"])

    #: Retention for raw events. Off by default: raw data is the ground
    #: truth and deleting it silently would make features unreproducible.
    retention_enabled: bool = False
    retention_days: int = 0

    @property
    def database_path(self) -> Path:
        return self.data_dir / "android_timeline.sqlite3"

    @property
    def pepper_path(self) -> Path:
        return self.data_dir / "token_pepper"

    def to_public_dict(self) -> dict[str, Any]:
        """Diagnostics-safe view. Never includes a token."""
        return {
            "data_dir": str(self.data_dir),
            "log_level": self.log_level,
            "max_batch_bytes": self.max_batch_bytes,
            "max_batch_events": self.max_batch_events,
            "max_event_bytes": self.max_event_bytes,
            "features_enabled": self.features_enabled,
            "timezone": self.timezone,
            "publish_entities": self.publish_entities,
            "mcp_enabled": self.mcp_enabled,
            "mcp_path": self.mcp_path,
            "retention_enabled": self.retention_enabled,
            "trust_ingress_admin": self.trust_ingress_admin,
            "admin_token_configured": bool(self.admin_token),
            "supervisor_token_configured": bool(self.supervisor_token),
        }


def _read_options(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_settings(options_path: Path | str | None = None) -> Settings:
    """Build settings from ``/data/options.json`` plus environment overrides."""
    data_dir = Path(os.environ.get("ANDROID_TIMELINE_DATA_DIR", "/data"))
    options = _read_options(Path(options_path) if options_path else data_dir / "options.json")

    settings = Settings(data_dir=data_dir)

    settings.log_level = str(
        os.environ.get("ANDROID_TIMELINE_LOG_LEVEL", options.get("log_level", "INFO"))
    ).upper()
    settings.max_batch_bytes = _env_int(
        "ANDROID_TIMELINE_MAX_BATCH_BYTES",
        int(options.get("max_batch_bytes", settings.max_batch_bytes)),
    )
    settings.max_batch_events = _env_int(
        "ANDROID_TIMELINE_MAX_BATCH_EVENTS",
        int(options.get("max_batch_events", settings.max_batch_events)),
    )
    settings.rate_limit_requests = _env_int(
        "ANDROID_TIMELINE_RATE_LIMIT",
        int(options.get("rate_limit_requests", settings.rate_limit_requests)),
    )
    settings.features_enabled = _env_bool(
        "ANDROID_TIMELINE_FEATURES_ENABLED",
        bool(options.get("features_enabled", settings.features_enabled)),
    )
    settings.timezone = str(
        os.environ.get("ANDROID_TIMELINE_TIMEZONE", options.get("timezone", "UTC"))
    )
    settings.publish_entities = _env_bool(
        "ANDROID_TIMELINE_PUBLISH_ENTITIES",
        bool(options.get("publish_entities", settings.publish_entities)),
    )
    settings.mcp_enabled = _env_bool(
        "ANDROID_TIMELINE_MCP_ENABLED",
        bool(options.get("mcp_enabled", settings.mcp_enabled)),
    )
    settings.trust_ingress_admin = _env_bool(
        "ANDROID_TIMELINE_TRUST_INGRESS_ADMIN",
        bool(options.get("trust_ingress_admin", settings.trust_ingress_admin)),
    )
    settings.retention_enabled = _env_bool(
        "ANDROID_TIMELINE_RETENTION_ENABLED",
        bool(options.get("retention_enabled", settings.retention_enabled)),
    )
    settings.retention_days = _env_int(
        "ANDROID_TIMELINE_RETENTION_DAYS",
        int(options.get("retention_days", settings.retention_days)),
    )

    # Secrets come from the environment only. Supervisor injects
    # SUPERVISOR_TOKEN; the admin token is an app option the user sets.
    settings.supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    settings.admin_token = os.environ.get(
        "ANDROID_TIMELINE_ADMIN_TOKEN", str(options.get("admin_token", ""))
    ).strip()

    return settings
