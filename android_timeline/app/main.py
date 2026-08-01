"""FastAPI application.

Endpoint groups and who may call them:

* ``/api/v1/health`` -- unauthenticated. CI and Home Assistant's watchdog
  need it before any credential exists.
* ``/api/v1/events/batch`` and ``/api/v1/devices/{id}/status`` -- a device
  bearer token, scoped to that device.
* everything under ``/api/v1/admin`` and the analysis endpoints -- admin,
  which means an ingress request (Supervisor has already authenticated the
  user) or a configured admin token.
* ``/mcp`` -- the read-only MCP server, behind the same admin check.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.routing import Mount

from . import FEATURE_VERSION, PROTOCOL_VERSION, SERVER_VERSION
from .auth import AuthError, RateLimiter, TokenManager
from .config import Settings, load_settings
from .coverage import coverage_summary, find_gaps
from .database import Database
from .features import (
    compute_daily_features,
    compute_hourly_features,
    register_feature_definitions,
)
from .home_assistant import publish_entities
from .ingestion import device_status, ingest_batch
from .mcp_server import build_mcp_server
from .models import EventBatch, iso_utc, parse_iso_utc, utc_now
from .timeline import build_day_timeline, yesterday_bounds

__all__ = ["create_app"]

logger = logging.getLogger("android_timeline")

MAINTENANCE_INTERVAL_SECONDS = 300


# ----------------------------------------------------------------------
# request models
# ----------------------------------------------------------------------


class EnrollRequest(BaseModel):
    device_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    display_name: str = Field(default="", max_length=128)
    label: str = Field(default="", max_length=64)


class RotateRequest(BaseModel):
    device_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")


# ----------------------------------------------------------------------
# application
# ----------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    settings = settings or load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database = Database(settings.database_path)
    register_feature_definitions(database)
    tokens = TokenManager(database, settings.pepper_path)
    limiter = RateLimiter(
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
    mcp = build_mcp_server(database, settings) if settings.mcp_enabled else None

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        maintenance: asyncio.Task[None] | None = None
        async with contextlib.AsyncExitStack() as stack:
            if mcp is not None:
                # A mounted MCP app's own lifespan never runs, so the host
                # application has to start its session manager. Without
                # this the first /mcp request fails.
                await stack.enter_async_context(mcp.session_manager.run())
            maintenance = asyncio.create_task(_maintenance_loop(database, settings))
            try:
                yield
            finally:
                maintenance.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await maintenance
        database.close()

    app = FastAPI(
        title="Android Timeline",
        version=SERVER_VERSION,
        description=(
            "Ingestion, feature engineering and read-only MCP access for "
            "Android Timeline data. Experimental."
        ),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.tokens = tokens
    app.state.limiter = limiter

    # -- auth dependencies ---------------------------------------------

    def _bearer(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        return authorization.split(" ", 1)[1].strip()

    def require_device(
        request: Request,
        authorization: str | None = Header(default=None),
        x_device_id: str | None = Header(default=None),
    ) -> str:
        if not x_device_id:
            raise HTTPException(400, "X-Device-ID header is required")
        try:
            limiter.check(f"device:{x_device_id}")
            tokens.verify(x_device_id, _bearer(authorization))
        except AuthError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        request.state.device_id = x_device_id
        return x_device_id

    def require_admin(
        request: Request,
        authorization: str | None = Header(default=None),
        x_ingress_path: str | None = Header(default=None),
    ) -> bool:
        # Supervisor authenticates the Home Assistant user before proxying
        # an ingress request, so its presence is a valid admin signal --
        # but only because this app publishes no port by default.
        if settings.trust_ingress_admin and x_ingress_path is not None:
            return True
        if settings.admin_token:
            try:
                supplied = _bearer(authorization)
            except HTTPException:
                raise HTTPException(401, "admin authentication required") from None
            import hmac

            if hmac.compare_digest(supplied, settings.admin_token):
                return True
        client = request.client.host if request.client else "unknown"
        logger.warning("admin request refused from %s", client)
        raise HTTPException(403, "admin authentication required")

    # -- error handling -------------------------------------------------

    @app.exception_handler(ValidationError)
    async def _validation_handler(_request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "schema validation failed", "errors": exc.errors()[:20]},
        )

    # -- health ----------------------------------------------------------

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        """Unauthenticated liveness probe. Reveals no user data."""
        return {
            "status": "ok",
            "server_version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "feature_version": FEATURE_VERSION,
            "database_schema_version": database.schema_version(),
            "device_count": len(database.devices()),
            "mcp_enabled": settings.mcp_enabled,
            "time_utc": iso_utc(utc_now()),
        }

    # -- ingestion -------------------------------------------------------

    @app.post("/api/v1/events/batch")
    async def ingest(
        request: Request, device_id: str = Depends(require_device)
    ) -> JSONResponse:
        """Accept a batch of raw events. Idempotent on event_id."""
        raw = await request.body()
        if len(raw) > settings.max_batch_bytes:
            raise HTTPException(
                413,
                f"request body exceeds {settings.max_batch_bytes} bytes",
            )

        if request.headers.get("content-encoding", "").lower() == "gzip":
            import gzip

            try:
                raw = gzip.decompress(raw)
            except OSError as exc:
                raise HTTPException(400, f"malformed gzip body: {exc}") from exc
            if len(raw) > settings.max_batch_bytes:
                raise HTTPException(413, "decompressed body is too large")

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"invalid JSON: {exc}") from exc

        try:
            batch = EventBatch.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "schema validation failed",
                    "errors": [
                        {"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()[:20]
                    ],
                },
            )

        if batch.device_id != device_id:
            raise HTTPException(403, "batch device_id does not match the authenticated device")

        header_batch_id = request.headers.get("x-batch-id")
        if header_batch_id and header_batch_id != batch.batch_id:
            raise HTTPException(400, "X-Batch-ID does not match the body batch_id")

        result = ingest_batch(database, settings, batch)
        return JSONResponse(status_code=200, content=result.acknowledgement.model_dump())

    @app.get("/api/v1/devices/{device_id}/status")
    async def status(
        device_id: str, authenticated: str = Depends(require_device)
    ) -> dict[str, Any]:
        if device_id != authenticated:
            raise HTTPException(403, "a device may only read its own status")
        try:
            return device_status(database, device_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown device") from exc

    # -- analysis (admin) -------------------------------------------------

    @app.get("/api/v1/timeline/yesterday")
    async def timeline_yesterday(
        device_id: str | None = None, _admin: bool = Depends(require_admin)
    ) -> dict[str, Any]:
        target = _resolve_device(database, device_id)
        start, end, local_date, tz_name = yesterday_bounds(settings.timezone)
        return build_day_timeline(
            database, target, start, end, local_date=local_date, tz_name=tz_name
        )

    @app.get("/api/v1/timeline/{local_date}")
    async def timeline_for_date(
        local_date: str,
        device_id: str | None = None,
        _admin: bool = Depends(require_admin),
    ) -> dict[str, Any]:
        from datetime import datetime

        from .timeline import resolve_timezone

        target = _resolve_device(database, device_id)
        tzinfo, tz_name = resolve_timezone(settings.timezone)
        try:
            day = datetime.fromisoformat(local_date).date()
        except ValueError as exc:
            raise HTTPException(400, "local_date must be an ISO date") from exc
        start = datetime(day.year, day.month, day.day, tzinfo=tzinfo)
        return build_day_timeline(
            database,
            target,
            start.astimezone(utc_now().tzinfo),
            (start + timedelta(days=1)).astimezone(utc_now().tzinfo),
            local_date=day.isoformat(),
            tz_name=tz_name,
        )

    @app.get("/api/v1/features/hourly")
    async def hourly(
        start_utc: str,
        end_utc: str,
        device_id: str | None = None,
        _admin: bool = Depends(require_admin),
    ) -> dict[str, Any]:
        target = _resolve_device(database, device_id)
        _validate_window(start_utc, end_utc)
        return {
            "device_id": target,
            "features": database.hourly_features(target, start_utc, end_utc),
        }

    @app.get("/api/v1/coverage")
    async def coverage_endpoint(
        start_utc: str,
        end_utc: str,
        device_id: str | None = None,
        _admin: bool = Depends(require_admin),
    ) -> dict[str, Any]:
        target = _resolve_device(database, device_id)
        start, end = _validate_window(start_utc, end_utc)
        return {
            "summary": coverage_summary(database, target, start, end),
            "gaps": find_gaps(database, target, start, end),
        }

    # -- admin ------------------------------------------------------------

    @app.post("/api/v1/admin/devices")
    async def enroll(
        body: EnrollRequest, _admin: bool = Depends(require_admin)
    ) -> dict[str, Any]:
        """Enroll a device and return its token. Shown exactly once."""
        token_id, token = tokens.enroll(
            body.device_id, display_name=body.display_name, label=body.label
        )
        logger.info("enrolled device %s (token %s)", body.device_id, token_id)
        return {
            "device_id": body.device_id,
            "token_id": token_id,
            "token": token,
            "warning": (
                "This token is shown once and is not recoverable. Only a "
                "keyed hash is stored. Copy it into the collector's token "
                "file now."
            ),
        }

    @app.post("/api/v1/admin/devices/{device_id}/rotate")
    async def rotate(device_id: str, _admin: bool = Depends(require_admin)) -> dict[str, Any]:
        if database.device(device_id) is None:
            raise HTTPException(404, "unknown device")
        token_id, token = tokens.rotate(device_id)
        return {
            "device_id": device_id,
            "token_id": token_id,
            "token": token,
            "warning": "Previous tokens for this device are now revoked.",
        }

    @app.delete("/api/v1/admin/tokens/{token_id}")
    async def revoke(token_id: str, _admin: bool = Depends(require_admin)) -> dict[str, Any]:
        return {"token_id": token_id, "revoked": tokens.revoke(token_id)}

    @app.get("/api/v1/admin/devices")
    async def list_devices(_admin: bool = Depends(require_admin)) -> dict[str, Any]:
        return {
            "devices": [
                {**device, "tokens": tokens.tokens(device["device_id"])}
                for device in database.devices()
            ]
        }

    @app.post("/api/v1/admin/recompute")
    async def recompute(
        device_id: str | None = None,
        hours: int = 48,
        _admin: bool = Depends(require_admin),
    ) -> dict[str, Any]:
        target = _resolve_device(database, device_id)
        hours = max(1, min(int(hours), 24 * 31))
        end = utc_now()
        start = end - timedelta(hours=hours)
        rows = compute_hourly_features(database, target, start, end)
        return {"device_id": target, "hourly_rows": rows, "hours": hours}

    # -- diagnostic view --------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(_admin: bool = Depends(require_admin)) -> str:
        """A deliberately minimal status page. No user data is rendered."""
        devices = database.devices()
        rows = "".join(
            f"<tr><td><code>{d['device_id']}</code></td>"
            f"<td>{d['last_seen_utc'] or 'never'}</td>"
            f"<td>{database.count_events(d['device_id'])}</td></tr>"
            for d in devices
        )
        return f"""<!doctype html>
<meta charset="utf-8">
<title>Android Timeline</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.5; }}
 table {{ border-collapse: collapse; }} td, th {{ padding: .35rem .75rem;
 border-bottom: 1px solid #ccc; text-align: left; }}
 .note {{ color: #555; font-size: .9rem; }}
</style>
<h1>Android Timeline</h1>
<p>Server {SERVER_VERSION}, protocol {PROTOCOL_VERSION}, features
   v{FEATURE_VERSION}. Schema {database.schema_version()}.</p>
<h2>Devices</h2>
<table><tr><th>Device</th><th>Last seen (UTC)</th><th>Events</th></tr>
{rows or '<tr><td colspan="3">No devices enrolled yet.</td></tr>'}</table>
<p class="note">This page is a diagnostic only. Use the MCP server or the
   API for analysis; no personal data is rendered here.</p>
"""

    # -- MCP --------------------------------------------------------------

    if mcp is not None:
        from mcp.server.transport_security import TransportSecuritySettings

        mcp_app = mcp.streamable_http_app(
            # Behind Home Assistant ingress the Host header is Home
            # Assistant's, not localhost, and the transport's DNS-rebinding
            # guard would otherwise answer every request with a 421.
            # Supervisor is the thing actually controlling that header.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
            streamable_http_path="/",
        )

        @app.middleware("http")
        async def _guard_mcp(request: Request, call_next: Any) -> Any:
            if request.url.path.startswith(settings.mcp_path):
                ingress = request.headers.get("x-ingress-path")
                authorization = request.headers.get("authorization")
                allowed = settings.trust_ingress_admin and ingress is not None
                if not allowed and settings.admin_token and authorization:
                    import hmac

                    supplied = authorization.split(" ", 1)[-1].strip()
                    allowed = hmac.compare_digest(supplied, settings.admin_token)
                if not allowed:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "MCP access requires admin authentication"},
                    )
            return await call_next(request)

        app.router.routes.append(Mount(settings.mcp_path, app=mcp_app))
        app.state.mcp = mcp

    return app


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _resolve_device(database: Database, device_id: str | None) -> str:
    """Resolve an optional device_id, defaulting when only one is enrolled."""
    if device_id:
        if database.device(device_id) is None:
            raise HTTPException(404, "unknown device")
        return device_id
    devices = database.devices()
    if not devices:
        raise HTTPException(404, "no devices are enrolled")
    if len(devices) > 1:
        raise HTTPException(400, "device_id is required when more than one device is enrolled")
    return str(devices[0]["device_id"])


def _validate_window(start_utc: str, end_utc: str) -> tuple[Any, Any]:
    try:
        start = parse_iso_utc(start_utc)
        end = parse_iso_utc(end_utc)
    except ValueError as exc:
        raise HTTPException(400, f"invalid timestamp: {exc}") from exc
    if end <= start:
        raise HTTPException(400, "end_utc must be after start_utc")
    if end - start > timedelta(days=31):
        raise HTTPException(400, "window must not exceed 31 days")
    return start, end


async def _maintenance_loop(database: Database, settings: Settings) -> None:
    """Recompute recent features, publish entities, apply retention."""
    while True:
        try:
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
            if settings.features_enabled:
                end = utc_now()
                start = end - timedelta(hours=settings.feature_recompute_hours)
                for device in database.devices():
                    compute_hourly_features(database, str(device["device_id"]), start, end)
                    day_start, _, local_date, _tz = yesterday_bounds(settings.timezone)
                    compute_daily_features(
                        database,
                        str(device["device_id"]),
                        day_start,
                        local_date=local_date,
                    )
            await publish_entities(database, settings)
            if settings.retention_enabled:
                removed = database.apply_retention(
                    enabled=True, older_than_days=settings.retention_days
                )
                if removed:
                    logger.info("retention removed %d raw events", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("maintenance cycle failed; continuing")


def get_app() -> FastAPI:  # pragma: no cover - container entry point
    return create_app()
