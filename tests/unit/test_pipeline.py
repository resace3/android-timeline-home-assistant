"""Database, ingestion, coverage and feature computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from android_timeline.app.config import Settings
from android_timeline.app.coverage import compute_coverage, coverage_summary, find_gaps
from android_timeline.app.database import Database
from android_timeline.app.features import (
    FEATURE_DEFINITIONS,
    compute_daily_features,
    compute_hourly_features,
)
from android_timeline.app.ingestion import ingest_batch
from android_timeline.app.models import EventBatch, iso_utc

DEVICE = "device-test-001"
DAY_START = datetime(2026, 3, 15, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)


def make_batch(events: list[dict[str, Any]], batch_id: str = "batch-test-0001") -> EventBatch:
    return EventBatch.model_validate(
        {
            "protocol_version": 1,
            "batch_id": batch_id,
            "device_id": DEVICE,
            "collector_version": "0.1.0",
            "created_time_utc": "2026-03-15T00:00:00Z",
            "events": events,
        }
    )


@pytest.fixture
def loaded(
    database: Database, settings: Settings, synthetic_day: dict[str, Any]
) -> dict[str, Any]:
    """A database holding the whole synthetic day."""
    database.upsert_device(DEVICE)
    events = synthetic_day["events"]
    for start in range(0, len(events), 200):
        ingest_batch(
            database,
            settings,
            make_batch(events[start : start + 200], f"batch-test-{start:04d}"),
        )
    return synthetic_day


class TestDatabase:
    def test_migrations_create_every_table(self, database: Database) -> None:
        assert {
            "devices",
            "device_tokens",
            "raw_events",
            "received_batches",
            "batch_event_acknowledgements",
            "hourly_features",
            "daily_features",
            "feature_definitions",
            "collector_heartbeats",
            "data_coverage",
            "interventions",
            "schema_migrations",
        } <= database.table_names()

    def test_migrations_are_idempotent(self, database: Database) -> None:
        assert database.migrate() == 1
        assert database.migrate() == 1

    def test_data_survives_reopening(self, settings: Settings) -> None:
        with Database(settings.database_path) as first:
            first.upsert_device(DEVICE)
        with Database(settings.database_path) as second:
            assert second.device(DEVICE) is not None

    def test_retention_is_off_by_default(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        ingest_batch(database, settings, make_batch(synthetic_day["events"][:10]))
        assert database.apply_retention(enabled=False, older_than_days=1) == 0
        assert database.count_events(DEVICE) > 0


class TestIngestion:
    def test_stores_every_unique_event_once(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        result = ingest_batch(database, settings, make_batch(synthetic_day["events"]))

        expected_unique = synthetic_day["expected"]["unique_event_ids"]
        assert len(result.stored_ids) == expected_unique
        assert database.count_events(DEVICE) == expected_unique
        # The batch contained one exact duplicate; it must not become a row.
        assert synthetic_day["expected"]["duplicate_rows"] == 1

    def test_replaying_the_same_batch_stores_nothing_new(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        batch = make_batch(synthetic_day["events"][:50])
        first = ingest_batch(database, settings, batch)
        second = ingest_batch(database, settings, batch)

        assert second.is_replay is True
        assert second.stored_ids == []
        assert len(second.duplicate_ids) == len(first.stored_ids)
        assert database.count_events(DEVICE) == len(first.stored_ids)

    def test_same_events_in_a_new_batch_store_nothing_new(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        events = synthetic_day["events"][:50]
        first = ingest_batch(database, settings, make_batch(events, "batch-test-aaaa"))
        second = ingest_batch(database, settings, make_batch(events, "batch-test-bbbb"))

        assert second.stored_ids == []
        assert database.count_events(DEVICE) == len(first.stored_ids)

    def test_every_event_gets_an_explicit_verdict(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        events = synthetic_day["events"][:20]
        ack = ingest_batch(database, settings, make_batch(events)).acknowledgement
        verdicts = {a.event_id for a in ack.accepted} | {r.event_id for r in ack.rejected}
        assert verdicts == {e["event_id"] for e in events}

    def test_a_mismatched_device_id_is_rejected_not_stored(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        events = [dict(e) for e in synthetic_day["events"][:3]]
        events[0]["device_id"] = "device-test-999"
        result = ingest_batch(database, settings, make_batch(events))

        assert events[0]["event_id"] in result.rejected_ids
        # Its siblings still land.
        assert len(result.stored_ids) == 2

    def test_a_future_dated_event_is_rejected(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        events = [dict(e) for e in synthetic_day["events"][:2]]
        events[0]["event_time_utc"] = "2099-01-01T00:00:00Z"
        events[0]["event_id"] = "event-from-the-future"
        result = ingest_batch(database, settings, make_batch(events))
        assert "event-from-the-future" in result.rejected_ids

    def test_heartbeats_are_projected(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        ingest_batch(database, settings, make_batch(synthetic_day["events"]))
        heartbeat = database.latest_heartbeat(DEVICE)
        assert heartbeat is not None
        assert "battery" in heartbeat["enabled_collectors"]

    def test_timestamps_and_offsets_are_preserved_exactly(
        self, database: Database, settings: Settings, synthetic_day: dict[str, Any]
    ) -> None:
        database.upsert_device(DEVICE)
        original = synthetic_day["events"][0]
        ingest_batch(database, settings, make_batch([original]))
        stored = database.event(original["event_id"])
        assert stored is not None
        assert stored["event_time_utc"] == original["event_time_utc"]
        assert stored["collected_time_utc"] == original["collected_time_utc"]
        assert stored["timezone_offset_minutes"] == original["timezone_offset_minutes"]


class TestCoverage:
    def test_the_two_hour_gap_is_detected(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        gaps = find_gaps(database, DEVICE, DAY_START, DAY_END)
        assert len(gaps) == 1
        assert gaps[0]["hours"] == 2
        assert gaps[0]["start_utc"] == loaded["expected"]["gap_start_utc"]
        assert gaps[0]["end_utc"] == loaded["expected"]["gap_end_utc"]

    def test_missing_hours_appear_as_rows_not_absences(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        rows = compute_coverage(database, DEVICE, DAY_START, DAY_END)
        assert len(rows) == 24
        missing = [r for r in rows if r["is_missing"]]
        assert {r["window_start_utc"] for r in missing} == {
            "2026-03-15T02:00:00Z",
            "2026-03-15T03:00:00Z",
        }

    def test_summary_reports_incompleteness(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        summary = coverage_summary(database, DEVICE, DAY_START, DAY_END)
        assert summary["hours_total"] == 24
        assert summary["hours_missing"] == 2
        assert summary["data_complete"] is False
        assert 0.0 < summary["coverage_proportion"] <= 1.0

    def test_expected_sources_come_from_the_heartbeat(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        summary = coverage_summary(database, DEVICE, DAY_START, DAY_END)
        assert "battery" in summary["expected_sources"]
        assert "sms" in summary["expected_sources"]


class TestFeatures:
    def test_every_definition_produces_a_row_per_hour(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        written = compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        assert written == 24 * len(FEATURE_DEFINITIONS)

    def test_battery_mean_is_plausible(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        rows = database.hourly_features(
            DEVICE,
            iso_utc(DAY_START),
            iso_utc(DAY_END),
            names=["battery_percentage_mean"],
        )
        values = [r["value_numeric"] for r in rows if r["value_numeric"] is not None]
        assert values
        assert all(0 <= v <= 100 for v in values)

    def test_charging_minutes_land_in_the_charging_window(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        rows = {
            r["window_start_utc"]: r["value_numeric"]
            for r in database.hourly_features(
                DEVICE, iso_utc(DAY_START), iso_utc(DAY_END), names=["charging_minutes"]
            )
        }
        assert rows["2026-03-15T06:00:00Z"] > 0
        assert rows["2026-03-15T07:00:00Z"] > 0
        assert rows["2026-03-15T09:00:00Z"] == 0

    def test_wifi_minutes_drop_during_the_disconnected_window(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        rows = {
            r["window_start_utc"]: r["value_numeric"]
            for r in database.hourly_features(
                DEVICE,
                iso_utc(DAY_START),
                iso_utc(DAY_END),
                names=["wifi_connected_minutes"],
            )
        }
        assert rows["2026-03-15T12:00:00Z"] == 0
        assert rows["2026-03-15T13:00:00Z"] == 0
        assert rows["2026-03-15T10:00:00Z"] > 0

    def test_missing_data_flag_marks_the_gap(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        rows = {
            r["window_start_utc"]: r["value_numeric"]
            for r in database.hourly_features(
                DEVICE,
                iso_utc(DAY_START),
                iso_utc(DAY_END),
                names=["missing_data_flag"],
            )
        }
        assert rows["2026-03-15T02:00:00Z"] == 1.0
        assert rows["2026-03-15T03:00:00Z"] == 1.0
        assert rows["2026-03-15T00:00:00Z"] == 0.0

    def test_features_carry_provenance(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        row = database.hourly_features(
            DEVICE, iso_utc(DAY_START), iso_utc(DAY_END), names=["calls_count"]
        )[0]
        assert row["feature_version"] == 1
        assert row["window_start_utc"] and row["window_end_utc"]
        assert row["source_event_types"] == ["call_record"]
        assert row["computed_at_utc"]
        assert 0.0 <= row["coverage_proportion"] <= 1.0

    def test_recomputing_does_not_touch_raw_events(
        self, database: Database, loaded: dict[str, Any]
    ) -> None:
        before = database.count_events(DEVICE)
        compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        assert database.count_events(DEVICE) == before

    def test_daily_rollup(self, database: Database, loaded: dict[str, Any]) -> None:
        compute_hourly_features(database, DEVICE, DAY_START, DAY_END)
        written = compute_daily_features(database, DEVICE, DAY_START, local_date="2026-03-15")
        assert written == len(FEATURE_DEFINITIONS)

        rows = {
            r["feature_name"]: r
            for r in database.daily_features(DEVICE, "2026-03-15", "2026-03-15")
        }
        assert rows["calls_count"]["value_numeric"] == loaded["expected"]["calls"]
        assert rows["sms_count"]["value_numeric"] == loaded["expected"]["sms"]
        # Two missing hours in the day.
        assert rows["missing_data_flag"]["value_numeric"] == 2.0


class TestSchemaFile:
    def test_requirements_match_pyproject(self) -> None:
        import tomllib

        root = Path(__file__).resolve().parents[2]
        pinned = {
            line.strip()
            for line in (root / "android_timeline" / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.startswith("#")
        }
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        assert pinned == set(project["project"]["dependencies"])
