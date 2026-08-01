"""Contract test against a narrow mock Supervisor.

This mock implements exactly one endpoint -- ``POST /core/api/states/{id}``
-- because that is the only Supervisor API this app calls. Building a fake
Home Assistant would prove nothing and rot immediately.

What is asserted:

* the app talks to the documented URL shape
* it presents the Supervisor token as a bearer credential
* it publishes only low-cardinality summaries, never one entity per event
* a Supervisor failure never propagates into ingestion
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from android_timeline.app.config import Settings  # noqa: E402
from android_timeline.app.database import Database  # noqa: E402
from android_timeline.app.home_assistant import (  # noqa: E402
    build_entity_states,
    publish_entities,
)
from android_timeline.app.ingestion import ingest_batch  # noqa: E402
from android_timeline.app.models import EventBatch  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))
from synthetic_day import build_synthetic_day  # noqa: E402

DEVICE = "device-test-001"


class MockSupervisor:
    """Records every state write the app attempts."""

    def __init__(self, *, fail: bool = False) -> None:
        self.writes: list[dict[str, Any]] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/core/api/states/"), request.url.path
        self.writes.append(
            {
                "entity_id": request.url.path.rsplit("/", 1)[-1],
                "authorization": request.headers.get("authorization"),
                "body": request.read().decode(),
            }
        )
        if self.fail:
            return httpx.Response(502, json={"message": "supervisor is unhappy"})
        return httpx.Response(200, json={"entity_id": "ok"})


@pytest.fixture
def loaded(tmp_path: Path) -> tuple[Database, Settings]:
    settings = Settings(
        data_dir=tmp_path / "data",
        timezone="UTC",
        publish_entities=True,
        supervisor_token="synthetic-supervisor-token",
        supervisor_url="http://supervisor",
    )
    database = Database(settings.database_path)
    database.upsert_device(DEVICE)

    day = build_synthetic_day()
    events = day["events"]
    for start in range(0, len(events), 200):
        ingest_batch(
            database,
            settings,
            EventBatch.model_validate(
                {
                    "protocol_version": 1,
                    "batch_id": f"batch-harness-{start:04d}",
                    "device_id": DEVICE,
                    "collector_version": "0.1.0",
                    "created_time_utc": "2026-03-15T00:00:00Z",
                    "events": events[start : start + 200],
                }
            ),
        )
    return database, settings


class TestEntityShape:
    def test_only_summary_entities_are_produced(
        self, loaded: tuple[Database, Settings]
    ) -> None:
        database, settings = loaded
        states = build_entity_states(database, settings, DEVICE)

        # One entity per statistic, not per raw event.
        assert 5 < len(states) < 20, len(states)
        assert database.count_events(DEVICE) > 100

    def test_entity_ids_are_namespaced_and_stable(
        self, loaded: tuple[Database, Settings]
    ) -> None:
        database, settings = loaded
        states = build_entity_states(database, settings, DEVICE)
        assert all(
            key.startswith(("sensor.android_timeline_", "binary_sensor.android_timeline_"))
            for key in states
        )
        assert build_entity_states(database, settings, DEVICE).keys() == states.keys()

    def test_no_entity_carries_a_credential(self, loaded: tuple[Database, Settings]) -> None:
        database, settings = loaded
        states = build_entity_states(database, settings, DEVICE)
        rendered = str(states)
        assert settings.supervisor_token not in rendered
        assert "token" not in rendered.lower()

    def test_binary_sensors_use_the_documented_polarity(
        self, loaded: tuple[Database, Settings]
    ) -> None:
        database, settings = loaded
        states = build_entity_states(database, settings, DEVICE)
        slug = DEVICE.replace("-", "_")
        complete = states[f"binary_sensor.android_timeline_{slug}_data_complete_today"]
        # device_class 'problem': on means there IS a problem, and the
        # synthetic day has a two-hour gap.
        assert complete["attributes"]["device_class"] == "problem"
        assert complete["state"] == "on"


class TestSupervisorContract:
    async def test_states_are_written_with_a_bearer_token(
        self, loaded: tuple[Database, Settings]
    ) -> None:
        database, settings = loaded
        supervisor = MockSupervisor()
        transport = httpx.MockTransport(supervisor.handler)

        async with httpx.AsyncClient(transport=transport) as client:
            written = await publish_entities(database, settings, client=client)

        assert written == len(supervisor.writes) > 0
        for write in supervisor.writes:
            assert write["authorization"] == "Bearer synthetic-supervisor-token"
            assert '"state"' in write["body"]

    async def test_a_supervisor_failure_is_survivable(
        self, loaded: tuple[Database, Settings]
    ) -> None:
        database, settings = loaded
        supervisor = MockSupervisor(fail=True)
        transport = httpx.MockTransport(supervisor.handler)

        async with httpx.AsyncClient(transport=transport) as client:
            written = await publish_entities(database, settings, client=client)

        # It tried, nothing was written, and no exception escaped.
        assert written == 0
        assert supervisor.writes

    async def test_a_transport_error_is_survivable(
        self, loaded: tuple[Database, Settings]
    ) -> None:
        database, settings = loaded

        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("supervisor unreachable")

        async with httpx.AsyncClient(transport=httpx.MockTransport(explode)) as client:
            assert await publish_entities(database, settings, client=client) == 0

    async def test_publishing_is_skipped_without_a_supervisor_token(
        self, loaded: tuple[Database, Settings]
    ) -> None:
        database, settings = loaded
        settings.supervisor_token = ""
        supervisor = MockSupervisor()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(supervisor.handler)
        ) as client:
            assert await publish_entities(database, settings, client=client) == 0
        assert supervisor.writes == []

    async def test_publishing_is_skipped_when_disabled(
        self, loaded: tuple[Database, Settings]
    ) -> None:
        database, settings = loaded
        settings.publish_entities = False
        supervisor = MockSupervisor()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(supervisor.handler)
        ) as client:
            assert await publish_entities(database, settings, client=client) == 0
        assert supervisor.writes == []


class TestNoUnnecessaryPrivileges:
    def test_the_app_declares_no_supervisor_role(self) -> None:
        import yaml

        config = yaml.safe_load(
            (REPO_ROOT / "android_timeline" / "config.yaml").read_text(encoding="utf-8")
        )
        assert config.get("hassio_api") is False
        assert "hassio_role" not in config
        assert config.get("homeassistant_api") is True

    def test_the_app_requests_no_host_access(self) -> None:
        import yaml

        config = yaml.safe_load(
            (REPO_ROOT / "android_timeline" / "config.yaml").read_text(encoding="utf-8")
        )
        for forbidden in (
            "host_network",
            "host_pid",
            "host_dbus",
            "privileged",
            "full_access",
            "docker_api",
            "devices",
            "video",
            "audio",
            "gpio",
            "uart",
            "usb",
        ):
            assert forbidden not in config, f"{forbidden} should not be requested"

    def test_the_app_maps_no_home_assistant_directory(self) -> None:
        import yaml

        config = yaml.safe_load(
            (REPO_ROOT / "android_timeline" / "config.yaml").read_text(encoding="utf-8")
        )
        assert "map" not in config, "no Home Assistant directory should be mounted"

    def test_the_app_publishes_no_port(self) -> None:
        import yaml

        config = yaml.safe_load(
            (REPO_ROOT / "android_timeline" / "config.yaml").read_text(encoding="utf-8")
        )
        # Ingress only. This is what makes trusting an ingress request safe.
        assert "ports" not in config
        assert config.get("ingress") is True


def test_yesterday_window_is_a_full_local_day() -> None:
    from android_timeline.app.timeline import yesterday_bounds

    now = datetime(2026, 3, 16, 9, 30, tzinfo=UTC)
    start, end, local_date, tz_name = yesterday_bounds("UTC", now=now)
    assert end - start == timedelta(days=1)
    assert local_date == "2026-03-15"
    assert tz_name == "UTC"
