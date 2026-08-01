"""The HTTP surface, end to end through the real ASGI app."""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from android_timeline.app.config import Settings

pytestmark = pytest.mark.integration

DEVICE = "device-test-001"
BATCH_PATH = "/api/v1/events/batch"


def device_headers(token: str, batch_id: str = "batch-test-0001") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Device-ID": DEVICE,
        "X-Batch-ID": batch_id,
        "Content-Type": "application/json",
    }


def unique(events: list[dict[str, Any]]) -> int:
    """How many distinct events a slice contains.

    The synthetic day deliberately repeats one event, and after sorting the
    repeat lands near the front, so a slice of N events is not N distinct
    observations.
    """
    return len({e["event_id"] for e in events})


def make_body(
    events: list[dict[str, Any]], batch_id: str = "batch-test-0001"
) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "batch_id": batch_id,
        "device_id": DEVICE,
        "collector_version": "0.1.0",
        "created_time_utc": "2026-03-15T00:00:00Z",
        "events": events,
    }


@pytest.fixture
def enrolled_client(
    client: TestClient, admin_headers: dict[str, str]
) -> tuple[TestClient, str]:
    response = client.post(
        "/api/v1/admin/devices",
        json={"device_id": DEVICE, "display_name": "Synthetic test device"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return client, response.json()["token"]


class TestHealth:
    def test_health_needs_no_credentials(self, client: TestClient) -> None:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["protocol_version"] == 1
        assert body["database_schema_version"] >= 1

    def test_health_leaks_no_user_data(self, client: TestClient) -> None:
        body = client.get("/api/v1/health").json()
        assert "devices" not in body
        assert "token" not in json.dumps(body).lower()


class TestEnrollment:
    def test_token_is_returned_once_with_a_warning(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = client.post(
            "/api/v1/admin/devices",
            json={"device_id": DEVICE},
            headers=admin_headers,
        ).json()
        assert body["token"].startswith("atl_")
        assert "once" in body["warning"]

    def test_enrollment_requires_admin(self, client: TestClient) -> None:
        response = client.post("/api/v1/admin/devices", json={"device_id": DEVICE})
        assert response.status_code in (401, 403)

    def test_an_ingress_request_counts_as_admin(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/admin/devices",
            json={"device_id": "device-test-002"},
            headers={"X-Ingress-Path": "/api/hassio_ingress/abc"},
        )
        assert response.status_code == 200

    def test_listing_devices_never_returns_a_token_value(
        self, enrolled_client: tuple[TestClient, str], admin_headers: dict[str, str]
    ) -> None:
        client, token = enrolled_client
        body = client.get("/api/v1/admin/devices", headers=admin_headers).text
        assert token not in body
        assert "token_hash" not in body


class TestIngestion:
    def test_authenticated_batch_is_stored(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        events = synthetic_day["events"][:20]
        response = client.post(
            BATCH_PATH, json=make_body(events), headers=device_headers(token)
        )
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["counts"]["received"] == 20
        assert body["counts"]["stored"] + body["counts"]["duplicate"] == 20
        assert body["counts"]["rejected"] == 0
        assert body["protocol_version"] == 1
        assert body["batch_id"] == "batch-test-0001"

    def test_missing_token_is_rejected(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, _ = enrolled_client
        response = client.post(
            BATCH_PATH,
            json=make_body(synthetic_day["events"][:1]),
            headers={"X-Device-ID": DEVICE},
        )
        assert response.status_code == 401

    def test_invalid_token_is_rejected(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, _ = enrolled_client
        response = client.post(
            BATCH_PATH,
            json=make_body(synthetic_day["events"][:1]),
            headers=device_headers("atl_not_a_real_token"),
        )
        assert response.status_code == 401

    def test_missing_device_header_is_rejected(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        response = client.post(
            BATCH_PATH,
            json=make_body(synthetic_day["events"][:1]),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400

    def test_batch_id_header_must_match_the_body(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        headers = device_headers(token, batch_id="batch-test-mismatch")
        response = client.post(
            BATCH_PATH, json=make_body(synthetic_day["events"][:1]), headers=headers
        )
        assert response.status_code == 400

    def test_invalid_json_is_rejected(self, enrolled_client: tuple[TestClient, str]) -> None:
        client, token = enrolled_client
        response = client.post(BATCH_PATH, content=b"{not json", headers=device_headers(token))
        assert response.status_code == 400

    def test_schema_violation_is_rejected_with_detail(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        events = [dict(synthetic_day["events"][0])]
        events[0]["event_time_utc"] = "2026-03-15 00:00:00"  # no Z, not RFC 3339
        response = client.post(
            BATCH_PATH, json=make_body(events), headers=device_headers(token)
        )
        assert response.status_code == 422
        assert "errors" in response.json()

    def test_unknown_field_is_rejected(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        events = [dict(synthetic_day["events"][0])]
        events[0]["surprise"] = 1
        response = client.post(
            BATCH_PATH, json=make_body(events), headers=device_headers(token)
        )
        assert response.status_code == 422

    def test_unknown_quality_flag_is_rejected(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        events = [dict(synthetic_day["events"][0])]
        events[0]["quality_flags"] = ["totally_made_up"]
        response = client.post(
            BATCH_PATH, json=make_body(events), headers=device_headers(token)
        )
        assert response.status_code == 422

    def test_oversized_request_is_rejected(
        self, enrolled_client: tuple[TestClient, str], settings: Settings
    ) -> None:
        client, token = enrolled_client
        oversized = b"x" * (settings.max_batch_bytes + 1)
        response = client.post(BATCH_PATH, content=oversized, headers=device_headers(token))
        assert response.status_code == 413

    def test_too_many_events_is_rejected(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        template = synthetic_day["events"][0]
        events = []
        for index in range(1001):
            event = dict(template)
            event["event_id"] = f"event-bulk-{index:05d}"
            events.append(event)
        response = client.post(
            BATCH_PATH, json=make_body(events), headers=device_headers(token)
        )
        assert response.status_code == 422

    def test_gzip_bodies_are_accepted(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        body = gzip.compress(json.dumps(make_body(synthetic_day["events"][:10])).encode())
        headers = device_headers(token)
        headers["Content-Encoding"] = "gzip"
        response = client.post(BATCH_PATH, content=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["counts"]["stored"] == unique(synthetic_day["events"][:10])

    def test_a_device_cannot_upload_for_another_device(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        body = make_body(synthetic_day["events"][:1])
        body["device_id"] = "device-test-002"
        response = client.post(BATCH_PATH, json=body, headers=device_headers(token))
        assert response.status_code == 403

    def test_sql_injection_in_a_payload_is_stored_as_text(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        event = dict(synthetic_day["events"][0])
        event["event_id"] = "event-injection-0001"
        event["payload"] = {"note": "'; DROP TABLE raw_events; --"}
        response = client.post(
            BATCH_PATH, json=make_body([event]), headers=device_headers(token)
        )
        assert response.status_code == 200
        assert client.get("/api/v1/health").json()["status"] == "ok"


class TestIdempotency:
    def test_replaying_a_batch_reports_duplicates(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        body = make_body(synthetic_day["events"][:15])
        first = client.post(BATCH_PATH, json=body, headers=device_headers(token)).json()
        second = client.post(BATCH_PATH, json=body, headers=device_headers(token)).json()

        events = synthetic_day["events"][:15]
        assert first["counts"]["stored"] == unique(events)
        assert second["counts"]["stored"] == 0
        # Every event in the request gets a verdict, duplicates included.
        assert second["counts"]["duplicate"] == len(events)
        assert all(a["status"] == "duplicate" for a in second["accepted"])

    def test_same_events_under_a_new_batch_id(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        events = synthetic_day["events"][:15]
        client.post(
            BATCH_PATH,
            json=make_body(events, "batch-test-aaaa"),
            headers=device_headers(token, "batch-test-aaaa"),
        )
        second = client.post(
            BATCH_PATH,
            json=make_body(events, "batch-test-bbbb"),
            headers=device_headers(token, "batch-test-bbbb"),
        ).json()

        assert second["counts"]["duplicate"] == len(events)
        assert second["counts"]["stored"] == 0


class TestDeviceStatus:
    def test_a_device_can_read_its_own_status(
        self, enrolled_client: tuple[TestClient, str], synthetic_day: dict[str, Any]
    ) -> None:
        client, token = enrolled_client
        client.post(
            BATCH_PATH,
            json=make_body(synthetic_day["events"][:10]),
            headers=device_headers(token),
        )
        body = client.get(
            f"/api/v1/devices/{DEVICE}/status",
            headers={"Authorization": f"Bearer {token}", "X-Device-ID": DEVICE},
        ).json()
        assert body["device_id"] == DEVICE
        assert body["total_events"] == unique(synthetic_day["events"][:10])
        assert "token" not in json.dumps(body).lower()

    def test_a_device_cannot_read_another_devices_status(
        self, enrolled_client: tuple[TestClient, str]
    ) -> None:
        client, token = enrolled_client
        response = client.get(
            "/api/v1/devices/device-test-002/status",
            headers={"Authorization": f"Bearer {token}", "X-Device-ID": DEVICE},
        )
        assert response.status_code == 403


class TestTokenLifecycle:
    def test_rotation_invalidates_the_previous_token(
        self,
        enrolled_client: tuple[TestClient, str],
        admin_headers: dict[str, str],
        synthetic_day: dict[str, Any],
    ) -> None:
        client, old_token = enrolled_client
        rotated = client.post(
            f"/api/v1/admin/devices/{DEVICE}/rotate", headers=admin_headers
        ).json()

        body = make_body(synthetic_day["events"][:1])
        assert (
            client.post(BATCH_PATH, json=body, headers=device_headers(old_token)).status_code
            == 401
        )
        assert (
            client.post(
                BATCH_PATH, json=body, headers=device_headers(rotated["token"])
            ).status_code
            == 200
        )

    def test_revocation_takes_effect_immediately(
        self,
        enrolled_client: tuple[TestClient, str],
        admin_headers: dict[str, str],
        synthetic_day: dict[str, Any],
    ) -> None:
        client, token = enrolled_client
        devices = client.get("/api/v1/admin/devices", headers=admin_headers).json()
        token_id = devices["devices"][0]["tokens"][0]["token_id"]

        client.delete(f"/api/v1/admin/tokens/{token_id}", headers=admin_headers)
        response = client.post(
            BATCH_PATH,
            json=make_body(synthetic_day["events"][:1]),
            headers=device_headers(token),
        )
        assert response.status_code == 401


class TestAnalysisEndpoints:
    def test_timeline_requires_admin(self, client: TestClient) -> None:
        assert client.get("/api/v1/timeline/yesterday").status_code in (401, 403)

    def test_coverage_window_is_bounded(
        self, enrolled_client: tuple[TestClient, str], admin_headers: dict[str, str]
    ) -> None:
        client, _ = enrolled_client
        response = client.get(
            "/api/v1/coverage",
            params={"start_utc": "2020-01-01T00:00:00Z", "end_utc": "2026-01-01T00:00:00Z"},
            headers=admin_headers,
        )
        assert response.status_code == 400

    def test_diagnostic_page_requires_admin(self, client: TestClient) -> None:
        assert client.get("/").status_code in (401, 403)

    def test_diagnostic_page_renders_for_admin(
        self, enrolled_client: tuple[TestClient, str], admin_headers: dict[str, str]
    ) -> None:
        client, _ = enrolled_client
        response = client.get("/", headers=admin_headers)
        assert response.status_code == 200
        assert DEVICE in response.text


class TestNoSecretsInResponses:
    def test_no_endpoint_echoes_the_admin_token(
        self,
        enrolled_client: tuple[TestClient, str],
        admin_headers: dict[str, str],
        settings: Settings,
    ) -> None:
        client, _ = enrolled_client
        for path in ("/api/v1/health", "/api/v1/admin/devices", "/"):
            response = client.get(path, headers=admin_headers)
            assert settings.admin_token not in response.text, path
