# Testing

What is validated, at which layer, and what is not.

## Layers

### Required, on every pull request

| Layer | Workflow | What it proves |
| --- | --- | --- |
| Unit | `ci.yml` | migrations, dedupe, auth, token lifecycle, rate limiting, ingestion verdicts, feature values, coverage, gap detection |
| API | `ci.yml` | the real ASGI app: auth, size limits, malformed JSON, schema violations, idempotency, per-device scoping, gzip |
| MCP | `ci.yml` | the real MCP protocol in memory: tool listing, bounds, pagination, redaction, read-only guarantees |
| Contract | `ci.yml` | Pydantic models and published JSON Schemas reach the same verdict on 17 valid/invalid cases; schema checksums |
| E2E in-process | `ci.yml` | synthetic day -> ingest -> features -> timeline -> MCP, across an app restart |
| Integration validation | `ci.yml` | `hassfest` on the custom integration; `helpers/info` on `config.yaml`; version agreement across three files |
| Container | `ci.yml`, `integration.yml` | the real image builds, boots, serves, survives a restart with its volume, and logs no credential |
| Mock Supervisor (B) | `integration.yml` | the one Supervisor endpoint this app calls, plus assertions that no wider privilege is requested |
| App image build (D) | `build-app.yml` | amd64 and aarch64 through the current official builder actions |
| **Cross-repository E2E** | `cross-repo-e2e.yml` | the **real collector** against the **real image** |

### Non-blocking

| Layer | Workflow | Why not required |
| --- | --- | --- |
| Home Assistant Core (C) | `integration.yml` | Depends on a large upstream image and its startup behaviour; a failure there is usually theirs, not ours |

### Not attempted

**Layer E, a supervised Home Assistant environment.** Running Supervisor in
CI needs privileged Docker-in-Docker or a full Home Assistant OS image in a
nested VM. Both are slow and unreliable enough that a green result would not
mean much, and an unreliable required check trains people to ignore it.

This leaves genuinely unvalidated:

- app installation through the real Supervisor store flow
- the real ingress proxy, including how Supervisor sets `X-Ingress-Path`
- app option parsing by Supervisor into `/data/options.json`
- s6-overlay supervision and restart behaviour under Supervisor
- backup and restore of the app's `/data` volume
- AppArmor enforcement of the profile Supervisor generates

These need a real Home Assistant installation. They are listed here rather
than glossed over.

## The cross-repository workflow

The most important test in either repository. It:

1. Checks out this repository and `android-timeline-termux` at a **pinned**
   revision (newest release tag by default, overridable by workflow input;
   it warns loudly if it has to fall back to `main`).
2. Diffs all three protocol schemas between the repositories and fails on
   any difference -- so a checksum file updated on only one side is still
   caught.
3. Asserts both sides declare the same `PROTOCOL_VERSION`.
4. Builds the app image from this repository's Dockerfile.
5. Installs the real collector and runs **its own** outbox and uploader.
6. Asserts, on the client side: the outbox is created, each synthetic event
   is stored once, the first upload is fully accepted, the queue empties,
   a replay sends nothing, a simulated outage accepts nothing but preserves
   the queue, the later retry succeeds, and an invalid token is refused
   without losing queued events.
7. Asserts, on the server side: 24 hour blocks, the two-hour gap present as
   explicit rows, one contiguous gap, correct calls/SMS counts, the
   charging and Wi-Fi windows reflected in features, and coverage reporting
   the day incomplete.
8. Restarts the container and re-asserts, proving persistence.
9. Copies the database out of the volume and queries it through MCP:
   tool listing, timeline, gaps, redaction, range bounds, and that a
   mutating call fails and changes nothing.
10. Greps every log for the device token and the admin token.

## The synthetic day

One deterministic day, 2026-03-15, generated identically in both
repositories. It deliberately contains:

- a **two-hour gap** (02:00-04:00 UTC) with no events from any source
- a **charging window** (06:00-08:00) and a **Wi-Fi outage** (12:00-14:00)
- a **late-arriving** event, observed at 05:00 but collected at 20:00
- an exact **duplicate** repeated in the stream
- events **replayed in a second batch** with different batch ids

Identifiers are reserved: `device-test-001`, `+1-555-0100`,
`example-network`, `place-home-synthetic`, `example.invalid`. A CI job fails
the build if a non-fictional phone number appears under `tests/`.

## Running it locally

```bash
python -m pip install -e ".[dev]"
pytest                        # everything
pytest tests/unit -v          # fast
pytest tests/e2e -v           # the full pipeline in process
pytest test-harness/mock-supervisor -v
```

The container and cross-repository layers need Docker:

```bash
docker compose -f test-harness/docker-compose.yml up --build
```

## What a green badge means

*The server side is correct against synthetic data produced by the real
collector, and the app image builds and runs.*

It does **not** mean anyone's phone has ever successfully uploaded to it.
