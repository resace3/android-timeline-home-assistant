"""Drive a running Android Timeline container through the whole pipeline.

Used by the integration workflow against the real container image, and by
the cross-repository workflow against the same image fed by the real
collector. Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEVICE = "device-test-001"
LOCAL_DATE = "2026-03-15"
ADMIN_TOKEN = "synthetic-ci-admin-token"  # noqa: S105 - CI fixture

_failures: list[str] = []


def check(name: str, condition: bool, detail: Any = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        _failures.append(name)


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - http(s) only, CI-local
        base_url.rstrip("/") + path, data=data, method=method
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")[:400]}


def admin(base_url: str, path: str, **kwargs: Any) -> tuple[int, Any]:
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {ADMIN_TOKEN}"
    return request(base_url, path, headers=headers, **kwargs)


def device_headers(token: str, batch_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Device-ID": DEVICE,
        "X-Batch-ID": batch_id,
    }


def load_events(path: str | None) -> list[dict[str, Any]]:
    if path:
        return list(json.loads(Path(path).read_text(encoding="utf-8")))
    from tests.fixtures.synthetic_day import build_synthetic_day

    return list(build_synthetic_day()["events"])


def upload(
    base_url: str, token: str, events: list[dict[str, Any]], prefix: str, chunk: int = 150
) -> list[dict[str, Any]]:
    acks = []
    for index in range(0, len(events), chunk):
        batch_id = f"{prefix}-{index:04d}"
        status, ack = request(
            base_url,
            "/api/v1/events/batch",
            method="POST",
            body={
                "protocol_version": 1,
                "batch_id": batch_id,
                "device_id": DEVICE,
                "collector_version": "0.1.0",
                "created_time_utc": "2026-03-15T00:00:00Z",
                "events": events[index : index + chunk],
            },
            headers=device_headers(token, batch_id),
        )
        if status != 200:
            check(f"upload {batch_id}", False, f"HTTP {status}: {ack}")
            return acks
        acks.append(ack)
    return acks


def verify(base_url: str) -> None:
    """Assertions that hold once the synthetic day is stored."""
    status, timeline = admin(base_url, f"/api/v1/timeline/{LOCAL_DATE}?device_id={DEVICE}")
    check("timeline endpoint responds", status == 200, f"HTTP {status}")
    if status != 200:
        return

    check("24 hour blocks", len(timeline["hour_blocks"]) == 24, len(timeline["hour_blocks"]))
    check("timezone is reported", timeline["timezone"] == "UTC", timeline["timezone"])
    check("feature version is reported", timeline["provenance"]["feature_version"] == 1)

    missing = [b for b in timeline["hour_blocks"] if b["is_missing"]]
    check("the two-hour gap is explicit", len(missing) == 2, len(missing))
    check(
        "the gap is where the fixture put it",
        {b["hour_start_utc"] for b in missing}
        == {"2026-03-15T02:00:00Z", "2026-03-15T03:00:00Z"},
    )
    check("one contiguous gap", timeline["missingness"]["gap_count"] == 1)
    check("data is reported incomplete", timeline["coverage"]["data_complete"] is False)

    daily = timeline["daily_features"]
    check(
        "calls feature computed",
        daily["calls_count"]["value"] == 2,
        daily["calls_count"]["value"],
    )
    check(
        "sms feature computed", daily["sms_count"]["value"] == 2, daily["sms_count"]["value"]
    )
    check(
        "missing-data flag counts both hours",
        daily["missing_data_flag"]["value"] == 2.0,
        daily["missing_data_flag"]["value"],
    )

    hourly = timeline["hour_blocks"]
    charging = {
        b["hour_start_utc"]: b["features"].get("charging_minutes", {}).get("value")
        for b in hourly
    }
    check(
        "charging appears in the charging window",
        (charging.get("2026-03-15T06:00:00Z") or 0) > 0,
        charging.get("2026-03-15T06:00:00Z"),
    )
    wifi = {
        b["hour_start_utc"]: b["features"].get("wifi_connected_minutes", {}).get("value")
        for b in hourly
    }
    check(
        "wifi drops in the disconnected window",
        wifi.get("2026-03-15T12:00:00Z") == 0,
        wifi.get("2026-03-15T12:00:00Z"),
    )

    status, coverage = admin(
        base_url,
        f"/api/v1/coverage?device_id={DEVICE}"
        "&start_utc=2026-03-15T00:00:00Z&end_utc=2026-03-16T00:00:00Z",
    )
    check("coverage endpoint responds", status == 200, f"HTTP {status}")
    if status == 200:
        check("coverage reports two missing hours", coverage["summary"]["hours_missing"] == 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--events", help="path to a JSON array of events")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="skip enrollment and upload; assert only on stored data",
    )
    args = parser.parse_args(argv)

    status, health = request(args.base_url, "/api/v1/health")
    check("health endpoint is unauthenticated", status == 200, f"HTTP {status}")
    check("protocol version is 1", health.get("protocol_version") == 1)

    if not args.verify_only:
        status, enrolled = admin(
            args.base_url,
            "/api/v1/admin/devices",
            method="POST",
            body={"device_id": DEVICE, "display_name": "CI synthetic device"},
        )
        check("device enrollment", status == 200, f"HTTP {status}: {enrolled}")
        if status != 200:
            return 1
        token = enrolled["token"]
        check("token is shown once with a warning", "once" in enrolled["warning"])

        status, _ = request(
            args.base_url,
            "/api/v1/events/batch",
            method="POST",
            body={
                "protocol_version": 1,
                "batch_id": "b",
                "device_id": DEVICE,
                "collector_version": "0.1.0",
                "created_time_utc": "2026-03-15T00:00:00Z",
                "events": [],
            },
            headers=device_headers("atl_wrong_token", "b"),
        )
        check("an invalid token is rejected", status == 401, f"HTTP {status}")

        events = load_events(args.events)
        first = upload(args.base_url, token, events, "batch-ci-a")
        stored = sum(a["counts"]["stored"] for a in first)
        duplicate = sum(a["counts"]["duplicate"] for a in first)
        unique = len({e["event_id"] for e in events})

        check(
            "first upload stores every unique event once",
            stored == unique,
            f"{stored} of {unique}",
        )
        check(
            "the in-batch duplicate is recognised",
            duplicate == len(events) - unique,
            duplicate,
        )

        replay = upload(args.base_url, token, events, "batch-ci-a")
        check(
            "replaying the same batch ids stores nothing",
            sum(a["counts"]["stored"] for a in replay) == 0,
        )

        again = upload(args.base_url, token, events, "batch-ci-b", chunk=97)
        check(
            "the same events in new batches store nothing",
            sum(a["counts"]["stored"] for a in again) == 0,
        )

        status, device = request(
            args.base_url,
            f"/api/v1/devices/{DEVICE}/status",
            headers={"Authorization": f"Bearer {token}", "X-Device-ID": DEVICE},
        )
        check(
            "device status reports the stored total",
            status == 200 and device["total_events"] == unique,
            device.get("total_events"),
        )
        check("device status contains no token", token not in json.dumps(device))

        status, _ = admin(
            args.base_url,
            f"/api/v1/admin/recompute?device_id={DEVICE}&hours=744",
            method="POST",
        )
        check("feature recompute is accepted", status == 200, f"HTTP {status}")

    verify(args.base_url)

    print()
    if _failures:
        print(f"::error::{len(_failures)} assertion(s) failed: {', '.join(_failures)}")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
