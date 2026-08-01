"""Shared fixtures.

Every test runs against a temporary database and synthetic data. Nothing
here reaches the network or a real Home Assistant.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "schemas"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(REPO_ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT))

from android_timeline.app.auth import TokenManager  # noqa: E402
from android_timeline.app.config import Settings  # noqa: E402
from android_timeline.app.database import Database  # noqa: E402

TEST_DEVICE = "device-test-001"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed entirely at a temporary directory."""
    return Settings(
        data_dir=tmp_path / "data",
        timezone="UTC",
        publish_entities=False,
        supervisor_token="",
        admin_token="synthetic-admin-token",
        trust_ingress_admin=True,
        mcp_enabled=True,
    )


@pytest.fixture
def database(settings: Settings) -> Iterator[Database]:
    with Database(settings.database_path) as db:
        yield db


@pytest.fixture
def tokens(database: Database, settings: Settings) -> TokenManager:
    return TokenManager(database, settings.pepper_path)


@pytest.fixture
def enrolled(tokens: TokenManager) -> tuple[str, str]:
    """An enrolled device and its plaintext token."""
    _, token = tokens.enroll(TEST_DEVICE, display_name="Synthetic test device")
    return TEST_DEVICE, token


@pytest.fixture
def synthetic_day() -> dict[str, Any]:
    from tests.fixtures.synthetic_day import build_synthetic_day

    return dict(build_synthetic_day())


@pytest.fixture
def schemas() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }


@pytest.fixture
def schema_validator(schemas: dict[str, dict[str, Any]]):  # type: ignore[no-untyped-def]
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry: Registry = Registry()  # type: ignore[type-arg]
    for document in schemas.values():
        registry = registry.with_resource(document["$id"], Resource.from_contents(document))

    def make(name: str) -> Draft202012Validator:
        return Draft202012Validator(schemas[name], registry=registry)

    return make


@pytest.fixture
def client(settings: Settings) -> Iterator[Any]:
    """A TestClient with the app's real lifespan running."""
    from fastapi.testclient import TestClient

    from android_timeline.app.main import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_token}"}
