"""The whole server-side pipeline, in one pass.

Upload -> store -> acknowledge -> features -> coverage -> timeline -> MCP,
including a process restart, using the same synthetic day the collector
produces.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from android_timeline.app.config import Settings
from android_timeline.app.database import Database
from android_timeline.app.main import create_app
from android_timeline.app.mcp_server import build_mcp_server

pytestmark = pytest.mark.e2e

DEVICE = "device-test-001"


def upload_day(
    client: TestClient, token: str, events: list[dict[str, Any]], chunk: int = 150
) -> list[dict[str, Any]]:
    responses = []
    for index in range(0, len(events), chunk):
        batch_id = f"batch-e2e-{index:04d}"
        response = client.post(
            "/api/v1/events/batch",
            json={
                "protocol_version": 1,
                "batch_id": batch_id,
                "device_id": DEVICE,
                "collector_version": "0.1.0",
                "created_time_utc": "2026-03-15T00:00:00Z",
                "events": events[index : index + chunk],
            },
            headers={
                "Authorization": f"Bearer {token}",
                "X-Device-ID": DEVICE,
                "X-Batch-ID": batch_id,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200, response.text
        responses.append(response.json())
    return responses


async def test_full_pipeline(
    settings: Settings, admin_headers: dict[str, str], synthetic_day: dict[str, Any]
) -> None:
    events = synthetic_day["events"]
    expected = synthetic_day["expected"]

    # -- 1. enroll and upload ------------------------------------------
    app = create_app(settings)
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/admin/devices",
            json={"device_id": DEVICE},
            headers=admin_headers,
        ).json()["token"]

        acks = upload_day(client, token, events)
        stored = sum(a["counts"]["stored"] for a in acks)
        duplicate = sum(a["counts"]["duplicate"] for a in acks)

        assert stored == expected["unique_event_ids"]
        assert duplicate == expected["duplicate_rows"]

        # -- 2. every event acknowledged exactly once -------------------
        acknowledged = [a["event_id"] for ack in acks for a in ack["accepted"]]
        assert len(acknowledged) == len(events)
        assert set(acknowledged) == {e["event_id"] for e in events}

        # -- 3. replay the whole day: nothing new -----------------------
        replay = upload_day(client, token, events)
        assert sum(a["counts"]["stored"] for a in replay) == 0

        # -- 4. same events, brand-new batch ids ------------------------
        again = upload_day(client, token, events, chunk=97)
        assert sum(a["counts"]["stored"] for a in again) == 0

        status = client.get(
            f"/api/v1/devices/{DEVICE}/status",
            headers={"Authorization": f"Bearer {token}", "X-Device-ID": DEVICE},
        ).json()
        assert status["total_events"] == expected["unique_event_ids"]

    # -- 5. restart: the data is still there ---------------------------
    restarted = create_app(settings)
    with TestClient(restarted) as client:
        health = client.get("/api/v1/health").json()
        assert health["device_count"] == 1

        timeline = client.get(
            "/api/v1/timeline/2026-03-15",
            params={"device_id": DEVICE},
            headers=admin_headers,
        ).json()

        # -- 6. timeline shape ------------------------------------------
        assert len(timeline["hour_blocks"]) == 24
        assert timeline["timezone"] == "UTC"
        assert timeline["local_date"] == "2026-03-15"
        assert timeline["provenance"]["feature_version"] == 1
        assert timeline["provenance"]["raw_events_are_immutable"] is True

        # -- 7. the two-hour gap is explicit, not an absence -------------
        missing = [b for b in timeline["hour_blocks"] if b["is_missing"]]
        assert len(missing) == 2
        assert {b["hour_start_utc"] for b in missing} == {
            "2026-03-15T02:00:00Z",
            "2026-03-15T03:00:00Z",
        }
        assert timeline["missingness"]["gap_count"] == 1
        assert timeline["gaps"][0]["hours"] == 2

        # -- 8. hourly features are present and provenanced --------------
        first_hour = timeline["hour_blocks"][0]["features"]
        assert "battery_percentage_mean" in first_hour
        assert first_hour["battery_percentage_mean"]["feature_version"] == 1
        assert first_hour["events_total"]["value"] > 0

        # -- 9. daily features ------------------------------------------
        daily = timeline["daily_features"]
        assert daily["calls_count"]["value"] == expected["calls"]
        assert daily["sms_count"]["value"] == expected["sms"]
        assert daily["missing_data_flag"]["value"] == 2.0

        # -- 10. coverage -------------------------------------------------
        coverage = client.get(
            "/api/v1/coverage",
            params={
                "device_id": DEVICE,
                "start_utc": "2026-03-15T00:00:00Z",
                "end_utc": "2026-03-16T00:00:00Z",
            },
            headers=admin_headers,
        ).json()
        assert coverage["summary"]["hours_missing"] == 2
        assert coverage["summary"]["data_complete"] is False

    # -- 11. MCP sees the same day --------------------------------------
    with Database(settings.database_path) as database:
        server = build_mcp_server(database, settings)
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool(
                "get_day_timeline", {"device_id": DEVICE, "local_date": "2026-03-15"}
            )
            body = json.loads(result.content[0].text)
            assert len(body["hour_blocks"]) == 24
            assert body["missingness"]["hours_missing"] == 2

            gaps = json.loads(
                (
                    await mcp_client.call_tool(
                        "find_data_gaps",
                        {
                            "device_id": DEVICE,
                            "start_utc": "2026-03-15T00:00:00Z",
                            "end_utc": "2026-03-16T00:00:00Z",
                        },
                    )
                )
                .content[0]
                .text
            )
            assert gaps["gap_count"] == 1

            # -- 12. MCP cannot mutate ---------------------------------
            before = database.count_events(DEVICE)
            failed = await mcp_client.call_tool("delete_events", {"device_id": DEVICE})
            assert failed.is_error
            assert database.count_events(DEVICE) == before


async def test_late_arriving_event_keeps_both_timestamps(
    settings: Settings, admin_headers: dict[str, str], synthetic_day: dict[str, Any]
) -> None:
    late_id = synthetic_day["expected"]["late_arrival_event_id"]
    original = next(e for e in synthetic_day["events"] if e["event_id"] == late_id)

    app = create_app(settings)
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/admin/devices", json={"device_id": DEVICE}, headers=admin_headers
        ).json()["token"]
        upload_day(client, token, synthetic_day["events"])

    with Database(settings.database_path) as database:
        stored = database.event(late_id)

    assert stored is not None
    assert stored["event_time_utc"] == original["event_time_utc"]
    assert stored["collected_time_utc"] == original["collected_time_utc"]
    assert stored["collected_time_utc"] > stored["event_time_utc"]
    assert stored["timezone_offset_minutes"] == -240


async def test_no_secret_appears_in_any_response(
    settings: Settings, admin_headers: dict[str, str], synthetic_day: dict[str, Any]
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/admin/devices", json={"device_id": DEVICE}, headers=admin_headers
        ).json()["token"]
        upload_day(client, token, synthetic_day["events"][:50])

        for path, params in (
            ("/api/v1/health", {}),
            ("/api/v1/admin/devices", {}),
            ("/api/v1/timeline/2026-03-15", {"device_id": DEVICE}),
            ("/", {}),
        ):
            response = client.get(path, params=params, headers=admin_headers)
            assert token not in response.text, path
            assert settings.admin_token not in response.text, path
