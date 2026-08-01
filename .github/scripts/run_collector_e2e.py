"""Drive the real Termux collector against a running app container.

Imports the collector package that CI just installed from
``resace3/android-timeline-termux`` and uses its own outbox and uploader --
nothing about the client side is reimplemented here.

Assertions cover the client half of the contract: idempotent upload, replay
safety, queue preservation across a network outage, and recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

_failures: list[str] = []


def check(name: str, condition: bool, detail: Any = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    if not condition:
        _failures.append(name)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def write_config(path: Path, base_url: str, data_dir: Path) -> None:
    path.write_text(
        f"""
[device]
device_id = "device-test-001"

[server]
base_url = "{base_url}"
allow_insecure_test_endpoint = true
max_batch_events = 150

[upload]
max_attempts = 3
initial_backoff_seconds = 0.05
max_backoff_seconds = 0.2

[privacy]
location_mode = "coarse"

[runtime]
data_dir = "{data_dir.as_posix()}"
""",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector-root", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    collector_root = Path(args.collector_root).resolve()
    sys.path.insert(0, str(collector_root))

    from android_timeline.config import load_config
    from android_timeline.database import Outbox
    from android_timeline.models import Event
    from android_timeline.uploader import Uploader

    # The collector's own synthetic day generator, from its checkout.
    sys.path.insert(0, str(collector_root / "tests" / "fixtures"))
    from synthetic_day import build_synthetic_day  # type: ignore[import-not-found]

    data_dir = Path(os.environ["ANDROID_TIMELINE_HOME"])
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config)
    write_config(config_path, args.base_url, data_dir)

    config = load_config(config_path)
    check("collector configuration loads", config.device.device_id == "device-test-001")

    day = build_synthetic_day()
    events = [Event.from_dict(e) for e in day["events"]]
    unique = len({e.event_id for e in events})

    with Outbox(config.database_path) as outbox:
        # -- 1. the collector creates its own outbox ---------------------
        check("SQLite outbox created", config.database_path.is_file())
        check("outbox schema applied", outbox.schema_version() >= 1)

        stored = outbox.add_events(events)
        check(
            "collector stores each synthetic event once",
            stored == unique and outbox.count_events() == unique,
            f"{stored} stored, {outbox.count_events()} rows, {len(events)} offered",
        )
        check(
            "the in-batch duplicate did not create a row",
            len(events) - unique == day["expected"]["duplicate_rows"],
        )

        uploader = Uploader(config, outbox, sleep=lambda _s: None)

        # -- 2. server is reachable --------------------------------------
        health = uploader.check_health()
        check("collector reaches the app", health.get("status") == "ok", health)

        # -- 3. first upload ---------------------------------------------
        result = uploader.upload_pending(max_batches=100)
        check(
            "first upload accepted every event",
            result.events_accepted == unique,
            result.to_dict(),
        )
        check("nothing was rejected", result.events_rejected == 0)
        check(
            "the local queue is now empty",
            outbox.stats().pending_events == 0,
            outbox.stats().to_dict(),
        )
        check(
            "acknowledged events are marked uploaded", outbox.stats().uploaded_events == unique
        )

        # -- 4. replay ----------------------------------------------------
        replayed = uploader.upload_pending(max_batches=100)
        check("a second run sends nothing", replayed.batches_sent == 0)

        # -- 5. offline: the queue must survive ---------------------------
        offline_events = [
            Event.create(
                device_id="device-test-001",
                source="tasker",
                event_type="screen_on",
                payload={"trigger": f"offline-{index}"},
            )
            for index in range(5)
        ]
        outbox.add_events(offline_events)

        original_url = config.server.base_url
        config.server.base_url = f"http://127.0.0.1:{free_port()}"
        outage = uploader.upload_pending(max_batches=2)
        check("an outage accepts nothing", outage.events_accepted == 0)
        check("an outage reports an error", bool(outage.errors), outage.errors)
        check(
            "the queue is preserved across the outage",
            outbox.stats().pending_events == len(offline_events),
            outbox.stats().to_dict(),
        )

        # -- 6. recovery ---------------------------------------------------
        config.server.base_url = original_url
        recovered = uploader.upload_pending(max_batches=100)
        check(
            "the retry after the outage succeeds",
            recovered.events_accepted == len(offline_events),
            recovered.to_dict(),
        )
        check("the queue drains", outbox.stats().pending_events == 0)

        # -- 7. an invalid token must be refused ---------------------------
        real_token = os.environ["ANDROID_TIMELINE_TOKEN"]
        os.environ["ANDROID_TIMELINE_TOKEN"] = "atl_definitely_not_valid"  # noqa: S105
        outbox.add_events(
            [
                Event.create(
                    device_id="device-test-001",
                    source="tasker",
                    event_type="screen_off",
                    payload={"trigger": "unauthorised"},
                )
            ]
        )
        refused = uploader.upload_pending(max_batches=1)
        check(
            "an invalid token is refused",
            refused.events_accepted == 0 and bool(refused.errors),
            refused.errors,
        )
        check("the queue survives an auth failure", outbox.stats().pending_events == 1)
        os.environ["ANDROID_TIMELINE_TOKEN"] = real_token
        uploader.upload_pending(max_batches=1)

        summary = outbox.stats().to_dict()
        Path(os.environ["RUNNER_TEMP"]).joinpath("collector-summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )

    print()
    if _failures:
        print(
            f"::error::{len(_failures)} collector assertion(s) failed: {', '.join(_failures)}"
        )
        return 1
    print("all collector assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
