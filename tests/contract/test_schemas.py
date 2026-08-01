"""Protocol contract.

This repository owns ``schemas/``. These tests assert that the Pydantic
models and the published JSON Schemas agree, so a change to one that is not
mirrored in the other fails here rather than on a phone.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from android_timeline.app import PROTOCOL_VERSION, SUPPORTED_SCHEMA_VERSIONS
from android_timeline.app.models import Acknowledgement, EventBatch, RawEvent

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"
CHECKSUM_FILE = SCHEMA_DIR / "PROTOCOL_SHA256SUMS"


def valid_event(**overrides: Any) -> dict[str, Any]:
    event = {
        "event_id": "11111111-2222-3333-4444-555555555555",
        "device_id": "device-test-001",
        "source": "battery",
        "event_type": "battery_sample",
        "event_time_utc": "2026-03-15T00:00:00Z",
        "collected_time_utc": "2026-03-15T00:00:01Z",
        "timezone_offset_minutes": -240,
        "schema_version": 1,
        "quality_flags": [],
        "payload": {"percentage": 55},
    }
    event.update(overrides)
    return event


class TestSchemaFiles:
    def test_expected_schemas_exist(self) -> None:
        assert {p.name for p in SCHEMA_DIR.glob("*.schema.json")} == {
            "event.schema.json",
            "batch.schema.json",
            "acknowledgement.schema.json",
        }

    def test_each_schema_is_valid(self, schemas: dict[str, dict[str, Any]]) -> None:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        for name, document in schemas.items():
            try:
                Draft202012Validator.check_schema(document)
            except SchemaError as exc:  # pragma: no cover
                pytest.fail(f"{name}: {exc}")

    def test_checksums_match(self) -> None:
        recorded = {}
        for line in CHECKSUM_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, _, name = line.partition("  ")
            recorded[name.strip()] = digest.strip()

        assert recorded
        for name, expected in recorded.items():
            actual = hashlib.sha256((SCHEMA_DIR / name).read_bytes()).hexdigest()
            assert actual == expected, (
                f"{name} changed. This repository is the source of truth for the "
                "protocol: update android-timeline-termux in the same release and "
                "regenerate PROTOCOL_SHA256SUMS."
            )

    def test_protocol_version_matches_the_schema_bound(
        self, schemas: dict[str, dict[str, Any]]
    ) -> None:
        bound = schemas["batch.schema.json"]["properties"]["protocol_version"]
        assert bound["minimum"] <= PROTOCOL_VERSION <= bound["maximum"]


class TestModelsAgreeWithSchemas:
    VALID: ClassVar[list[dict[str, Any]]] = [
        {},
        {"quality_flags": ["mocked", "coarse"]},
        {"timezone_offset_minutes": 0},
        {"payload": {}},
        {"payload": {"note": "'; DROP TABLE raw_events; --"}},
        {"event_time_utc": "2026-03-15T00:00:00.123Z"},
    ]

    INVALID: ClassVar[list[dict[str, Any]]] = [
        {"event_id": ""},
        {"event_id": "has spaces"},
        {"device_id": "../../etc/passwd"},
        {"source": "Battery"},
        {"event_type": "has-dashes"},
        {"event_time_utc": "2026-03-15T00:00:00"},
        {"event_time_utc": "2026-03-15T00:00:00+01:00"},
        {"timezone_offset_minutes": 2000},
        {"schema_version": 0},
        {"payload": []},
        {"quality_flags": ["totally_made_up"]},
    ]

    @pytest.mark.parametrize("overrides", VALID)
    def test_valid_cases_pass_both(
        self, overrides: dict[str, Any], schema_validator: Callable[[str], Any]
    ) -> None:
        event = valid_event(**overrides)
        RawEvent.model_validate(event)
        schema_validator("event.schema.json").validate(event)

    @pytest.mark.parametrize("overrides", INVALID)
    def test_invalid_cases_fail_both(
        self, overrides: dict[str, Any], schema_validator: Callable[[str], Any]
    ) -> None:
        event = valid_event(**overrides)
        with pytest.raises(Exception):  # noqa: B017 - pydantic raises its own
            RawEvent.model_validate(event)
        assert not schema_validator("event.schema.json").is_valid(event)

    def test_unknown_top_level_field_fails_both(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        event = valid_event(surprise=1)
        with pytest.raises(Exception):  # noqa: B017
            RawEvent.model_validate(event)
        assert not schema_validator("event.schema.json").is_valid(event)

    def test_supported_schema_versions_are_accepted(self) -> None:
        for version in SUPPORTED_SCHEMA_VERSIONS:
            RawEvent.model_validate(valid_event(schema_version=version))

    def test_an_unsupported_schema_version_is_rejected_clearly(self) -> None:
        with pytest.raises(Exception, match="unsupported schema_version"):
            RawEvent.model_validate(valid_event(schema_version=99))


class TestBatchAndAcknowledgement:
    def test_batch_round_trips_against_the_schema(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        batch = EventBatch.model_validate(
            {
                "protocol_version": 1,
                "batch_id": "batch-test-0001",
                "device_id": "device-test-001",
                "collector_version": "0.1.0",
                "created_time_utc": "2026-03-15T00:00:00Z",
                "events": [valid_event()],
            }
        )
        schema_validator("batch.schema.json").validate(batch.model_dump())

    def test_acknowledgement_round_trips_against_the_schema(
        self, schema_validator: Callable[[str], Any]
    ) -> None:
        ack = Acknowledgement.model_validate(
            {
                "protocol_version": 1,
                "batch_id": "batch-test-0001",
                "server_version": "0.1.0",
                "received_time_utc": "2026-03-15T00:00:00Z",
                "accepted": [{"event_id": "abc", "status": "stored"}],
                "rejected": [{"event_id": "def", "reason": "schema"}],
                "counts": {"received": 2, "stored": 1, "duplicate": 0, "rejected": 1},
            }
        )
        schema_validator("acknowledgement.schema.json").validate(ack.model_dump())

    def test_empty_batch_fails_both(self, schema_validator: Callable[[str], Any]) -> None:
        body = {
            "protocol_version": 1,
            "batch_id": "batch-test-0001",
            "device_id": "device-test-001",
            "collector_version": "0.1.0",
            "created_time_utc": "2026-03-15T00:00:00Z",
            "events": [],
        }
        with pytest.raises(Exception):  # noqa: B017
            EventBatch.model_validate(body)
        assert not schema_validator("batch.schema.json").is_valid(body)


class TestSyntheticDay:
    def test_every_event_validates(
        self, synthetic_day: dict[str, Any], schema_validator: Callable[[str], Any]
    ) -> None:
        validator = schema_validator("event.schema.json")
        for event in synthetic_day["events"]:
            validator.validate(event)
            RawEvent.model_validate(event)

    def test_uses_reserved_identifiers_only(self, synthetic_day: dict[str, Any]) -> None:
        import json

        text = json.dumps(synthetic_day)
        assert "device-test-001" in text
        for forbidden in ("@gmail", ".local", "nabu", "192.168."):
            assert forbidden not in text
