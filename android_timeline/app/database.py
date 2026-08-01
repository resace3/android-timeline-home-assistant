"""Persistent event store.

SQLite, in the app's ``/data`` directory. Chosen deliberately: a personal
deployment produces on the order of tens of thousands of events per day,
which SQLite handles comfortably, and it needs no second container, no
credentials and no backup story beyond Home Assistant's own.

Raw events are append-only. Features are derived and may be recomputed at
any time; recomputing never rewrites a raw event.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from .models import iso_utc, utc_now

__all__ = ["SCHEMA_VERSION", "Database", "split_statements"]

SCHEMA_VERSION = 1


def split_statements(script: str) -> list[str]:
    """Split a migration into statements (executescript would auto-commit)."""
    without_comments = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("--")
    )
    return [s.strip() for s in without_comments.split(";") if s.strip()]


_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_id      TEXT PRIMARY KEY,
            display_name   TEXT NOT NULL DEFAULT '',
            created_at_utc TEXT NOT NULL,
            last_seen_utc  TEXT,
            enabled        INTEGER NOT NULL DEFAULT 1,
            notes          TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS device_tokens (
            token_id       TEXT PRIMARY KEY,
            device_id      TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
            token_hash     TEXT NOT NULL,
            algorithm      TEXT NOT NULL DEFAULT 'hmac-sha256',
            label          TEXT NOT NULL DEFAULT '',
            created_at_utc TEXT NOT NULL,
            last_used_utc  TEXT,
            revoked_at_utc TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tokens_device
            ON device_tokens (device_id, revoked_at_utc);

        CREATE TABLE IF NOT EXISTS raw_events (
            event_id                TEXT PRIMARY KEY,
            device_id               TEXT NOT NULL,
            source                  TEXT NOT NULL,
            event_type              TEXT NOT NULL,
            event_time_utc          TEXT NOT NULL,
            collected_time_utc      TEXT NOT NULL,
            timezone_offset_minutes INTEGER NOT NULL,
            schema_version          INTEGER NOT NULL,
            quality_flags           TEXT NOT NULL DEFAULT '[]',
            payload                 TEXT NOT NULL DEFAULT '{}',
            received_at_utc         TEXT NOT NULL,
            first_batch_id          TEXT NOT NULL,
            protocol_version        INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_events_device_time
            ON raw_events (device_id, event_time_utc);
        CREATE INDEX IF NOT EXISTS idx_events_source_time
            ON raw_events (device_id, source, event_time_utc);
        CREATE INDEX IF NOT EXISTS idx_events_received
            ON raw_events (received_at_utc);

        CREATE TABLE IF NOT EXISTS received_batches (
            batch_id               TEXT PRIMARY KEY,
            device_id              TEXT NOT NULL,
            received_at_utc        TEXT NOT NULL,
            client_created_time_utc TEXT NOT NULL,
            collector_version      TEXT NOT NULL DEFAULT '',
            protocol_version       INTEGER NOT NULL DEFAULT 1,
            event_count            INTEGER NOT NULL DEFAULT 0,
            stored_count           INTEGER NOT NULL DEFAULT 0,
            duplicate_count        INTEGER NOT NULL DEFAULT 0,
            rejected_count         INTEGER NOT NULL DEFAULT 0,
            replay_count           INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_batches_device
            ON received_batches (device_id, received_at_utc);

        CREATE TABLE IF NOT EXISTS batch_event_acknowledgements (
            batch_id  TEXT NOT NULL,
            event_id  TEXT NOT NULL,
            status    TEXT NOT NULL,
            reason    TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (batch_id, event_id)
        );

        CREATE TABLE IF NOT EXISTS feature_definitions (
            feature_name       TEXT NOT NULL,
            feature_version    INTEGER NOT NULL,
            description        TEXT NOT NULL DEFAULT '',
            unit               TEXT NOT NULL DEFAULT '',
            source_event_types TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (feature_name, feature_version)
        );

        CREATE TABLE IF NOT EXISTS hourly_features (
            device_id           TEXT NOT NULL,
            feature_name        TEXT NOT NULL,
            feature_version     INTEGER NOT NULL,
            window_start_utc    TEXT NOT NULL,
            window_end_utc      TEXT NOT NULL,
            value_numeric       REAL,
            value_text          TEXT,
            source_event_types  TEXT NOT NULL DEFAULT '[]',
            coverage_proportion REAL NOT NULL DEFAULT 0.0,
            quality_flags       TEXT NOT NULL DEFAULT '[]',
            computed_at_utc     TEXT NOT NULL,
            PRIMARY KEY (device_id, feature_name, feature_version, window_start_utc)
        );

        CREATE INDEX IF NOT EXISTS idx_hourly_window
            ON hourly_features (device_id, window_start_utc);

        CREATE TABLE IF NOT EXISTS daily_features (
            device_id           TEXT NOT NULL,
            feature_name        TEXT NOT NULL,
            feature_version     INTEGER NOT NULL,
            local_date          TEXT NOT NULL,
            window_start_utc    TEXT NOT NULL,
            window_end_utc      TEXT NOT NULL,
            value_numeric       REAL,
            value_text          TEXT,
            source_event_types  TEXT NOT NULL DEFAULT '[]',
            coverage_proportion REAL NOT NULL DEFAULT 0.0,
            quality_flags       TEXT NOT NULL DEFAULT '[]',
            computed_at_utc     TEXT NOT NULL,
            PRIMARY KEY (device_id, feature_name, feature_version, local_date)
        );

        CREATE TABLE IF NOT EXISTS collector_heartbeats (
            event_id           TEXT PRIMARY KEY,
            device_id          TEXT NOT NULL,
            event_time_utc     TEXT NOT NULL,
            received_at_utc    TEXT NOT NULL,
            collector_version  TEXT NOT NULL DEFAULT '',
            queue_pending      INTEGER NOT NULL DEFAULT 0,
            enabled_collectors TEXT NOT NULL DEFAULT '[]',
            payload            TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_heartbeats_device_time
            ON collector_heartbeats (device_id, event_time_utc);

        CREATE TABLE IF NOT EXISTS data_coverage (
            device_id           TEXT NOT NULL,
            window_start_utc    TEXT NOT NULL,
            window_end_utc      TEXT NOT NULL,
            expected_sources    TEXT NOT NULL DEFAULT '[]',
            observed_sources    TEXT NOT NULL DEFAULT '[]',
            event_count         INTEGER NOT NULL DEFAULT 0,
            heartbeat_count     INTEGER NOT NULL DEFAULT 0,
            coverage_proportion REAL NOT NULL DEFAULT 0.0,
            is_missing          INTEGER NOT NULL DEFAULT 1,
            computed_at_utc     TEXT NOT NULL,
            PRIMARY KEY (device_id, window_start_utc)
        );

        CREATE TABLE IF NOT EXISTS interventions (
            intervention_id TEXT PRIMARY KEY,
            device_id       TEXT NOT NULL,
            name            TEXT NOT NULL,
            start_utc       TEXT NOT NULL,
            end_utc         TEXT,
            notes           TEXT NOT NULL DEFAULT '',
            created_at_utc  TEXT NOT NULL
        );
        """,
    ),
]


class Database:
    """Thin, explicit SQLite wrapper. No ORM, no lazy loading."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.migrate()

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
        return row

    # -- migrations ----------------------------------------------------

    def migrate(self) -> int:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version        INTEGER PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            )
            """
        )
        current = self.schema_version()
        for version, script in _MIGRATIONS:
            if version <= current:
                continue
            with self.transaction() as conn:
                for statement in split_statements(script):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at_utc) VALUES (?, ?)",
                    (version, iso_utc(utc_now())),
                )
            current = version
        return current

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        return int(row["v"]) if row else 0

    def table_names(self) -> set[str]:
        return {
            str(row["name"])
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    # -- devices -------------------------------------------------------

    def upsert_device(self, device_id: str, display_name: str = "") -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO devices (device_id, display_name, created_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    display_name = CASE
                        WHEN excluded.display_name != '' THEN excluded.display_name
                        ELSE devices.display_name END
                """,
                (device_id, display_name, iso_utc(utc_now())),
            )

    def touch_device(self, device_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE devices SET last_seen_utc = ? WHERE device_id = ?",
                (iso_utc(utc_now()), device_id),
            )

    def device(self, device_id: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        return dict(row) if row else None

    def devices(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query("SELECT * FROM devices ORDER BY device_id")]

    def set_device_enabled(self, device_id: str, enabled: bool) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE devices SET enabled = ? WHERE device_id = ?",
                (int(enabled), device_id),
            )

    # -- raw events ----------------------------------------------------

    def store_events(
        self, rows: Sequence[tuple[Any, ...]], batch_id: str
    ) -> tuple[list[str], list[str]]:
        """Insert events, reporting which were new and which were duplicates.

        ``INSERT OR IGNORE`` on the primary key is what makes replay safe:
        the same event arriving twice, in the same batch or a different one,
        can only ever occupy one row.
        """
        stored: list[str] = []
        duplicate: list[str] = []
        if not rows:
            return stored, duplicate

        with self.transaction() as conn:
            existing: set[str] = set()
            ids = [row[0] for row in rows]
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start : chunk_start + 500]
                placeholders = ",".join("?" * len(chunk))
                existing.update(
                    str(r["event_id"])
                    for r in conn.execute(
                        f"SELECT event_id FROM raw_events WHERE event_id IN ({placeholders})",  # noqa: S608  # nosec B608
                        chunk,
                    )
                )

            fresh = [row for row in rows if row[0] not in existing]
            conn.executemany(
                """
                INSERT OR IGNORE INTO raw_events (
                    event_id, device_id, source, event_type, event_time_utc,
                    collected_time_utc, timezone_offset_minutes, schema_version,
                    quality_flags, payload, received_at_utc, first_batch_id,
                    protocol_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                fresh,
            )
            stored = [row[0] for row in fresh]
            duplicate = [row[0] for row in rows if row[0] in existing]

        del batch_id  # recorded on the batch row, not per event
        return stored, duplicate

    def event(self, event_id: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM raw_events WHERE event_id = ?", (event_id,))
        return _event_row(row) if row else None

    def count_events(self, device_id: str | None = None) -> int:
        if device_id:
            row = self.query_one(
                "SELECT COUNT(*) AS c FROM raw_events WHERE device_id = ?", (device_id,)
            )
        else:
            row = self.query_one("SELECT COUNT(*) AS c FROM raw_events")
        return int(row["c"]) if row else 0

    def events_in_window(
        self,
        device_id: str,
        start_utc: str,
        end_utc: str,
        *,
        sources: Sequence[str] | None = None,
        limit: int = 5000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["device_id = ?", "event_time_utc >= ?", "event_time_utc < ?"]
        params: list[Any] = [device_id, start_utc, end_utc]
        if sources:
            clauses.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        params.extend([limit, offset])
        sql = (
            f"SELECT * FROM raw_events WHERE {' AND '.join(clauses)} "  # noqa: S608  # nosec B608
            "ORDER BY event_time_utc, event_id LIMIT ? OFFSET ?"
        )
        return [_event_row(row) for row in self.query(sql, params)]

    def latest_event(self, device_id: str, source: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM raw_events WHERE device_id = ? AND source = ? "
            "ORDER BY event_time_utc DESC, event_id DESC LIMIT 1",
            (device_id, source),
        )
        return _event_row(row) if row else None

    def sources(self, device_id: str | None = None) -> list[dict[str, Any]]:
        if device_id:
            rows = self.query(
                "SELECT source, COUNT(*) AS event_count, MIN(event_time_utc) AS first_utc, "
                "MAX(event_time_utc) AS last_utc FROM raw_events WHERE device_id = ? "
                "GROUP BY source ORDER BY source",
                (device_id,),
            )
        else:
            rows = self.query(
                "SELECT source, COUNT(*) AS event_count, MIN(event_time_utc) AS first_utc, "
                "MAX(event_time_utc) AS last_utc FROM raw_events GROUP BY source ORDER BY source"
            )
        return [dict(r) for r in rows]

    # -- batches -------------------------------------------------------

    def record_batch(self, values: dict[str, Any]) -> bool:
        """Record a batch. Returns ``False`` when this batch_id was replayed."""
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT batch_id FROM received_batches WHERE batch_id = ?",
                (values["batch_id"],),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE received_batches SET replay_count = replay_count + 1 "
                    "WHERE batch_id = ?",
                    (values["batch_id"],),
                )
                return False
            conn.execute(
                """
                INSERT INTO received_batches (
                    batch_id, device_id, received_at_utc, client_created_time_utc,
                    collector_version, protocol_version, event_count, stored_count,
                    duplicate_count, rejected_count
                ) VALUES (
                    :batch_id, :device_id, :received_at_utc, :client_created_time_utc,
                    :collector_version, :protocol_version, :event_count, :stored_count,
                    :duplicate_count, :rejected_count
                )
                """,
                values,
            )
            return True

    def record_acknowledgements(
        self, batch_id: str, entries: Sequence[tuple[str, str, str]]
    ) -> None:
        if not entries:
            return
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO batch_event_acknowledgements "
                "(batch_id, event_id, status, reason) VALUES (?, ?, ?, ?)",
                [(batch_id, event_id, status, reason) for event_id, status, reason in entries],
            )

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM received_batches WHERE batch_id = ?", (batch_id,))
        return dict(row) if row else None

    def batches(self, device_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.query(
                "SELECT * FROM received_batches WHERE device_id = ? "
                "ORDER BY received_at_utc DESC LIMIT ?",
                (device_id, limit),
            )
        ]

    # -- heartbeats ----------------------------------------------------

    def record_heartbeat(self, event: dict[str, Any]) -> None:
        payload = event["payload"]
        queue = payload.get("queue") or {}
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO collector_heartbeats (
                    event_id, device_id, event_time_utc, received_at_utc,
                    collector_version, queue_pending, enabled_collectors, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["device_id"],
                    event["event_time_utc"],
                    iso_utc(utc_now()),
                    str(payload.get("collector_version", "")),
                    int(queue.get("pending_events", 0) or 0),
                    json.dumps(payload.get("enabled_collectors") or []),
                    json.dumps(payload),
                ),
            )

    def latest_heartbeat(self, device_id: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM collector_heartbeats WHERE device_id = ? "
            "ORDER BY event_time_utc DESC LIMIT 1",
            (device_id,),
        )
        if not row:
            return None
        record = dict(row)
        record["enabled_collectors"] = json.loads(record["enabled_collectors"] or "[]")
        record["payload"] = json.loads(record["payload"] or "{}")
        return record

    def heartbeats_in_window(
        self, device_id: str, start_utc: str, end_utc: str
    ) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.query(
                "SELECT * FROM collector_heartbeats WHERE device_id = ? "
                "AND event_time_utc >= ? AND event_time_utc < ? ORDER BY event_time_utc",
                (device_id, start_utc, end_utc),
            )
        ]

    # -- features and coverage ------------------------------------------

    def upsert_feature_definition(
        self,
        feature_name: str,
        feature_version: int,
        description: str,
        unit: str,
        source_event_types: Sequence[str],
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO feature_definitions "
                "(feature_name, feature_version, description, unit, source_event_types) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    feature_name,
                    feature_version,
                    description,
                    unit,
                    json.dumps(list(source_event_types)),
                ),
            )

    def feature_definitions(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM feature_definitions ORDER BY feature_name, feature_version"
        )
        result = []
        for row in rows:
            record = dict(row)
            record["source_event_types"] = json.loads(record["source_event_types"] or "[]")
            result.append(record)
        return result

    def upsert_hourly_features(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO hourly_features (
                    device_id, feature_name, feature_version, window_start_utc,
                    window_end_utc, value_numeric, value_text, source_event_types,
                    coverage_proportion, quality_flags, computed_at_utc
                ) VALUES (
                    :device_id, :feature_name, :feature_version, :window_start_utc,
                    :window_end_utc, :value_numeric, :value_text, :source_event_types,
                    :coverage_proportion, :quality_flags, :computed_at_utc
                )
                """,
                rows,
            )
        return len(rows)

    def upsert_daily_features(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO daily_features (
                    device_id, feature_name, feature_version, local_date,
                    window_start_utc, window_end_utc, value_numeric, value_text,
                    source_event_types, coverage_proportion, quality_flags,
                    computed_at_utc
                ) VALUES (
                    :device_id, :feature_name, :feature_version, :local_date,
                    :window_start_utc, :window_end_utc, :value_numeric, :value_text,
                    :source_event_types, :coverage_proportion, :quality_flags,
                    :computed_at_utc
                )
                """,
                rows,
            )
        return len(rows)

    def hourly_features(
        self,
        device_id: str,
        start_utc: str,
        end_utc: str,
        *,
        names: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["device_id = ?", "window_start_utc >= ?", "window_start_utc < ?"]
        params: list[Any] = [device_id, start_utc, end_utc]
        if names:
            clauses.append(f"feature_name IN ({','.join('?' * len(names))})")
            params.extend(names)
        rows = self.query(
            f"SELECT * FROM hourly_features WHERE {' AND '.join(clauses)} "  # noqa: S608  # nosec B608
            "ORDER BY window_start_utc, feature_name",
            params,
        )
        return [_feature_row(r) for r in rows]

    def daily_features(
        self,
        device_id: str,
        start_date: str,
        end_date: str,
        *,
        names: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["device_id = ?", "local_date >= ?", "local_date <= ?"]
        params: list[Any] = [device_id, start_date, end_date]
        if names:
            clauses.append(f"feature_name IN ({','.join('?' * len(names))})")
            params.extend(names)
        rows = self.query(
            f"SELECT * FROM daily_features WHERE {' AND '.join(clauses)} "  # noqa: S608  # nosec B608
            "ORDER BY local_date, feature_name",
            params,
        )
        return [_feature_row(r) for r in rows]

    def upsert_coverage(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO data_coverage (
                    device_id, window_start_utc, window_end_utc, expected_sources,
                    observed_sources, event_count, heartbeat_count,
                    coverage_proportion, is_missing, computed_at_utc
                ) VALUES (
                    :device_id, :window_start_utc, :window_end_utc, :expected_sources,
                    :observed_sources, :event_count, :heartbeat_count,
                    :coverage_proportion, :is_missing, :computed_at_utc
                )
                """,
                rows,
            )
        return len(rows)

    def coverage(self, device_id: str, start_utc: str, end_utc: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM data_coverage WHERE device_id = ? AND window_start_utc >= ? "
            "AND window_start_utc < ? ORDER BY window_start_utc",
            (device_id, start_utc, end_utc),
        )
        result = []
        for row in rows:
            record = dict(row)
            record["expected_sources"] = json.loads(record["expected_sources"] or "[]")
            record["observed_sources"] = json.loads(record["observed_sources"] or "[]")
            record["is_missing"] = bool(record["is_missing"])
            result.append(record)
        return result

    # -- retention -----------------------------------------------------

    def apply_retention(self, *, enabled: bool, older_than_days: int) -> int:
        """Delete raw events older than the cutoff. Off unless enabled."""
        if not enabled or older_than_days < 1:
            return 0
        cutoff = iso_utc(utc_now() - timedelta(days=older_than_days))
        with self.transaction() as conn:
            before = conn.total_changes
            conn.execute("DELETE FROM raw_events WHERE event_time_utc < ?", (cutoff,))
            deleted = conn.total_changes - before
        return int(deleted)


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "device_id": row["device_id"],
        "source": row["source"],
        "event_type": row["event_type"],
        "event_time_utc": row["event_time_utc"],
        "collected_time_utc": row["collected_time_utc"],
        "timezone_offset_minutes": int(row["timezone_offset_minutes"]),
        "schema_version": int(row["schema_version"]),
        "quality_flags": json.loads(row["quality_flags"] or "[]"),
        "payload": json.loads(row["payload"] or "{}"),
        "received_at_utc": row["received_at_utc"],
    }


def _feature_row(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["source_event_types"] = json.loads(record["source_event_types"] or "[]")
    record["quality_flags"] = json.loads(record["quality_flags"] or "[]")
    return record
