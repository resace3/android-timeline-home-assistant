# Contributing

## Ground rules

1. **Never commit real data.** No real Home Assistant URLs, tokens, device
   identifiers, coordinates or phone numbers -- not in code, tests,
   fixtures, issues or screenshots. CI fails on credential-shaped strings
   anywhere in history. Use `device-test-001`, `+1-555-0100`,
   `example.invalid`, `place-home-synthetic`.
2. **Raw events are immutable.** Anything that rewrites, backfills or
   silently drops a stored event needs a very good argument and a migration.
3. **Never claim validation that did not happen.** The testing table in the
   README and `docs/testing.md` must stay accurate.
4. **Privileges only shrink.** Adding a Supervisor role, a port, a device
   mapping or a directory mount requires a rationale in the PR. Tests in
   `test-harness/mock-supervisor` assert the current minimal set.
5. **MCP stays read-only.** No tool may write, execute, or return
   unredacted sensitive fields. Tests enforce this by scanning tool names
   and schemas.

## Setup

```bash
python -m pip install -e ".[dev]"
```

Python 3.12+.

## Before you push

```bash
ruff format .
ruff check .
mypy
pytest
```

Run the pipeline locally:

```bash
docker compose -f test-harness/docker-compose.yml up --build
```

## Adding a feature to the pipeline

1. Add a `FeatureDefinition` to `features.py` with a description, unit,
   source event types and a daily aggregation.
2. Compute it in `_hour_features`. Emit `None` rather than `0` when there
   was nothing to measure -- zero and unknown are different.
3. Add assertions to `tests/unit/test_pipeline.py` using the synthetic day.
4. If the definition of an **existing** feature changes, bump
   `FEATURE_VERSION`. Old rows keep their version; nothing is rewritten.

## Changing the protocol

This repository is the source of truth for `schemas/`.

1. Change the schema here.
2. Update the Pydantic models to match; the contract tests fail otherwise.
3. Regenerate `schemas/PROTOCOL_SHA256SUMS` (`sha256sum schemas/*.schema.json`).
4. Copy the files byte-for-byte into `android-timeline-termux` and
   regenerate its checksums too.
5. Bump `PROTOCOL_VERSION` if the change is breaking, and release both
   repositories together. The cross-repository workflow fails until they
   agree.

## Pull requests

- Branch from `main`; direct pushes are blocked.
- All required checks must pass. Do not disable a test or add
  `continue-on-error` to get a merge.
- The Home Assistant Core layer is non-blocking by design. If you make it
  reliably green, say so and it can be promoted.
