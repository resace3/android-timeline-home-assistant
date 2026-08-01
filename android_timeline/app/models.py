"""Wire models.

These mirror ``schemas/*.schema.json``, which is the published contract and
the source of truth for both repositories. A contract test asserts the
Pydantic models and the JSON Schemas agree.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import SUPPORTED_SCHEMA_VERSIONS

__all__ = [
    "QUALITY_FLAGS",
    "AcceptedEvent",
    "Acknowledgement",
    "EventBatch",
    "RawEvent",
    "RejectedEvent",
    "iso_utc",
    "parse_iso_utc",
    "utc_now",
]

_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")

QUALITY_FLAGS = frozenset(
    {
        "collector_error",
        "command_unavailable",
        "permission_denied",
        "mocked",
        "experimental",
        "late_arrival",
        "redacted",
        "coarse",
        "partial",
        "clock_uncertain",
    }
)

IdStr = Annotated[str, Field(pattern=_ID_PATTERN)]
NameStr = Annotated[str, Field(pattern=_NAME_PATTERN)]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def floor_hour(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hour_range(start: datetime, end: datetime) -> list[datetime]:
    """Every hour boundary in ``[start, end)``."""
    cursor = floor_hour(start)
    hours: list[datetime] = []
    while cursor < end:
        hours.append(cursor)
        cursor += timedelta(hours=1)
    return hours


class RawEvent(BaseModel):
    """One immutable observation from a device."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: IdStr
    device_id: IdStr
    source: NameStr
    event_type: NameStr
    event_time_utc: str
    collected_time_utc: str
    timezone_offset_minutes: int = Field(ge=-1080, le=1080)
    schema_version: int = Field(ge=1)
    quality_flags: list[str] = Field(default_factory=list, max_length=16)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_time_utc", "collected_time_utc")
    @classmethod
    def _utc_timestamp(cls, value: str) -> str:
        if not _ISO_RE.match(value):
            raise ValueError(
                "must be an RFC 3339 UTC timestamp ending in 'Z' "
                "(local time without an offset is not accepted)"
            )
        return value

    @field_validator("quality_flags")
    @classmethod
    def _known_flags(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - QUALITY_FLAGS)
        if unknown:
            raise ValueError(f"unknown quality flag(s): {', '.join(unknown)}")
        return value

    @field_validator("schema_version")
    @classmethod
    def _supported_schema(cls, value: int) -> int:
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported schema_version {value}; this server accepts "
                f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        return value

    @property
    def event_time(self) -> datetime:
        return parse_iso_utc(self.event_time_utc)

    @property
    def collected_time(self) -> datetime:
        return parse_iso_utc(self.collected_time_utc)


class EventBatch(BaseModel):
    """The body of ``POST /api/v1/events/batch``."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    batch_id: IdStr
    device_id: IdStr
    collector_version: str = Field(min_length=1, max_length=64)
    created_time_utc: str
    events: list[RawEvent] = Field(min_length=1, max_length=1000)

    @field_validator("created_time_utc")
    @classmethod
    def _utc_timestamp(cls, value: str) -> str:
        if not _ISO_RE.match(value):
            raise ValueError("must be an RFC 3339 UTC timestamp ending in 'Z'")
        return value


class AcceptedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: IdStr
    #: ``duplicate`` means the server already held it. The client must treat
    #: that as success -- it is what makes retries safe.
    status: Literal["stored", "duplicate"]


class RejectedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(max_length=128)
    reason: str = Field(max_length=500)


class AcknowledgementCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    received: int = Field(ge=0)
    stored: int = Field(ge=0)
    duplicate: int = Field(ge=0)
    rejected: int = Field(ge=0)


class Acknowledgement(BaseModel):
    """The response to a batch upload: per-event, never per-batch."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    batch_id: IdStr
    server_version: str = Field(max_length=64)
    received_time_utc: str
    accepted: list[AcceptedEvent] = Field(default_factory=list)
    rejected: list[RejectedEvent] = Field(default_factory=list)
    counts: AcknowledgementCounts
