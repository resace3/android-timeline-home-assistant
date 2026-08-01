"""Enroll a synthetic device and upload the deterministic day.

Local development only. The real client-side behaviour (outbox, retries,
backoff, dead-lettering) is exercised in CI by the actual collector; this
just gets data into a locally running app.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8099").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "synthetic-local-admin-token")
DEVICE_ID = os.environ.get("DEVICE_ID", "device-test-001")
FIXTURE = Path("/repo/tests/fixtures")


def post(path: str, body: dict, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(  # noqa: S310 - local http only
        BASE_URL + path, data=json.dumps(body).encode(), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    for _ in range(60):
        try:
            with urllib.request.urlopen(  # noqa: S310
                BASE_URL + "/api/v1/health", timeout=5
            ) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(2)
    else:
        print("app never became healthy", file=sys.stderr)
        return 1

    status, enrolled = post(
        "/api/v1/admin/devices",
        {"device_id": DEVICE_ID, "display_name": "Synthetic local phone"},
        {"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    if status != 200:
        print(f"enrollment failed: HTTP {status} {enrolled}", file=sys.stderr)
        return 1
    token = enrolled["token"]
    print(f"enrolled {DEVICE_ID}")

    sys.path.insert(0, str(FIXTURE))
    from synthetic_day import build_synthetic_day  # type: ignore[import-not-found]

    events = build_synthetic_day()["events"]
    total_stored = 0
    for index in range(0, len(events), 150):
        batch_id = f"batch-local-{index:04d}"
        status, ack = post(
            "/api/v1/events/batch",
            {
                "protocol_version": 1,
                "batch_id": batch_id,
                "device_id": DEVICE_ID,
                "collector_version": "0.1.0",
                "created_time_utc": "2026-03-15T00:00:00Z",
                "events": events[index : index + 150],
            },
            {
                "Authorization": f"Bearer {token}",
                "X-Device-ID": DEVICE_ID,
                "X-Batch-ID": batch_id,
            },
        )
        if status != 200:
            print(f"upload failed: HTTP {status} {ack}", file=sys.stderr)
            return 1
        total_stored += ack["counts"]["stored"]

    print(f"uploaded {len(events)} events, {total_stored} newly stored")
    print(f"Open {BASE_URL}/ with the admin token to inspect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
