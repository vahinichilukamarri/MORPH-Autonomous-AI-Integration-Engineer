# Milestones

## v0.1-foundation

A runnable skeleton, two deliberately mismatched mock enterprise systems with real OpenAPI
contracts, deterministic failure injection, and the answer-key format MORPH is graded against.
There is no AI in this milestone.

### What exists

| Area | Where |
|---|---|
| Postgres 16 + pgvector, mock CRM, mock Support (compose, healthchecks, named volume) | `docker-compose.yml` |
| Backend: FastAPI `/health`, pydantic-settings, SQLAlchemy engine, Alembic (`0001` enables `vector`) | `backend/` |
| Frontend: Vite React-TS page that shows backend health | `frontend/` |
| Mock CRM (`Customer`, `X-API-Key`) and mock Support (`User`, Bearer), documented mismatches | `mock_systems/`, `mock_systems/README.md` |
| Failure injection under `/__admin` (own token, not in the public spec) | `mock_systems/common/faults.py` |
| Committed OpenAPI contracts | `mock_systems/openapi/` |
| Scenario / answer-key models, loader, JSON Schemas, first scenario | `bench/` |
| Developer commands | `scripts/dev.ps1` |
| CI (ruff, mypy, pytest, Schemathesis, tsc, eslint) | `.github/workflows/ci.yml` |

### Prerequisites

Docker Desktop (running), Python 3.12, Node 22+, and `uv` (`python -m pip install --user uv`).
`dev.ps1` falls back to `python -m uv` when `uv` is not on `PATH`.

### Run and verify (PowerShell, from the repo root)

1. Lint and test everything (no containers needed):

   ```powershell
   ./scripts/dev.ps1 lint
   ./scripts/dev.ps1 test
   ```

   Expect all linters clean and passing tests in `backend`, `mock_systems` (includes the
   Schemathesis conformance run against both OpenAPI specs) and `bench`.

2. Bring the stack up:

   ```powershell
   ./scripts/dev.ps1 up
   docker compose ps
   ```

   Expect `postgres`, `mock-crm` and `mock-support` to be `healthy`.

3. Check the backend and the frontend:

   ```powershell
   Invoke-RestMethod http://localhost:8000/health
   Invoke-RestMethod http://127.0.0.1:5173/api/health
   Start-Process http://localhost:5173
   ```

   Expect `status: ok, database: ok`, and the page to read "Backend: ok (database: ok)".

4. Call the mock systems:

   ```powershell
   Invoke-RestMethod "http://localhost:8101/customers?page_size=2" -Headers @{ 'X-API-Key' = 'crm-dev-key' }
   Invoke-RestMethod http://localhost:8102/openapi.json | Select-Object -ExpandProperty paths
   ```

5. Try failure injection (admin token from `.env.example`):

   ```powershell
   $admin = @{ 'X-Admin-Token' = 'admin-dev-token' }
   $crm = @{ 'X-API-Key' = 'crm-dev-key' }
   Invoke-RestMethod -Method Put http://localhost:8101/__admin/faults -Headers $admin `
     -ContentType 'application/json' -Body '{"seed": 7, "http_500_rate": 0.5}'
   1..6 | ForEach-Object {
     try { (Invoke-WebRequest http://localhost:8101/customers/C-1654 -Headers $crm -UseBasicParsing).StatusCode }
     catch { $_.Exception.Response.StatusCode.value__ }
   }
   Invoke-RestMethod -Method Post http://localhost:8101/__admin/reset -Headers $admin
   ```

   The same seed always yields the same sequence of 200 and 500 responses. Other fault fields:
   `http_429_rate` with `retry_after_seconds`, `latency_ms`, `timeout` with `timeout_seconds`,
   `malformed_json_rate`, `drop_fields`, and `contract_version` (`v1` or `v2`).

6. Validate the bench scenario and regenerate the schemas:

   ```powershell
   cd bench
   uv run python -c "from morph_bench.loader import discover; print([b.scenario.id for b in discover()])"
   uv run python -m morph_bench.export_schemas
   git status --short   # expect no changes: committed schemas are current
   cd ..
   ```

7. Reset the database, then shut down:

   ```powershell
   ./scripts/dev.ps1 reset-db
   ./scripts/dev.ps1 down
   ```

### Contract versions (for later drift work)

With `contract_version: "v2"` the CRM renames `phone` to `phone_number` and Support renames
`tier` to `serviceTier`, in requests, responses and the served OpenAPI document. A request
that still uses the old name is rejected with 422.

### Regenerating committed artefacts

Tests fail when these go stale:

```powershell
cd mock_systems; uv run python -m common.export_openapi; cd ..
cd bench; uv run python -m morph_bench.export_schemas; cd ..
```

## v0.2-discovery

OpenAPI contract -> internal system model -> versioned persistence -> field embeddings in
pgvector -> cross-system retrieval, plus a measured retrieval baseline. No LLM calls. Design
and rules: `docs/discovery.md`. Measured results: `docs/retrieval-baseline.md`.

### What exists

| Area | Where |
|---|---|
| System model, OpenAPI parser, versioned persistence (migration `0002`) | `backend/app/discovery/`, `backend/app/db_models.py` |
| Embedding providers (fastembed, fake), text templates, retrieval | `backend/app/embeddings/` |
| Discovery API (`/systems/...`, `/fields/{id}/similar`) | `backend/app/api/discovery.py` |
| `dev.ps1 ingest`, `dev.ps1 test-slow` | `scripts/dev.ps1` |
| Retrieval baseline script and report | `bench/scripts/retrieval_baseline.py`, `docs/retrieval-baseline.md` |
| Drifted (v2) contracts for the parser | `mock_systems/openapi/*.v2.json` |

### Run and verify (PowerShell, from the repo root)

1. Lint and test (the tests create and drop their own `morph_test` and `morph_bench_test`
   databases, so Postgres must be up: `docker compose up -d --wait postgres`):

   ```powershell
   ./scripts/dev.ps1 lint
   ./scripts/dev.ps1 test
   ```

   Slow tests that use the real embedding model (downloads it into `.cache/fastembed` on the
   first run; the command installs the optional `embeddings` dependency group itself):

   ```powershell
   ./scripts/dev.ps1 test-slow
   ```

2. Bring the stack up and ingest both mock systems from their running URLs:

   ```powershell
   ./scripts/dev.ps1 up
   ./scripts/dev.ps1 ingest crm http://localhost:8101/openapi.json
   ./scripts/dev.ps1 ingest support http://localhost:8102/openapi.json
   ```

   Each call prints a JSON summary: `system_id`, `version`, `created`, `spec_hash`, `entities`,
   `fields`, `fields_embedded`, `entities_embedded` and `embedding_model`. The first call also
   downloads the model. Ingesting the same spec again prints `created: false` and embeds
   nothing. A local file works too: `./scripts/dev.ps1 ingest crm mock_systems/openapi/crm.v1.json`.

3. Ingest a changed contract as a new version. Switch the CRM to contract v2, ingest, switch back:

   ```powershell
   $admin = @{ 'X-Admin-Token' = 'admin-dev-token' }
   Invoke-RestMethod -Method Put http://localhost:8101/__admin/faults -Headers $admin `
     -ContentType 'application/json' -Body '{"contract_version": "v2"}' | Out-Null
   ./scripts/dev.ps1 ingest crm http://localhost:8101/openapi.json
   Invoke-RestMethod -Method Post http://localhost:8101/__admin/reset -Headers $admin | Out-Null
   ```

   Expect `version: 2`, `created: true`.

4. Inspect what was stored:

   ```powershell
   $b = 'http://localhost:8000'
   Invoke-RestMethod "$b/systems" | ForEach-Object { $_ } | Format-Table
   Invoke-RestMethod "$b/systems/1/versions" | ForEach-Object { $_ } |
     Select-Object version, api_version, entities, spec_hash | Format-Table
   # latest version has phone_number; version 1 still has phone
   (Invoke-RestMethod "$b/systems/1/entities" | Where-Object name -eq 'Customer').fields.path -join ', '
   (Invoke-RestMethod "$b/systems/1/entities?version=1" | Where-Object name -eq 'Customer').fields.path -join ', '
   ```

5. Query similar fields. Take a field id from the entities response, then ask for its nearest
   fields in the other systems (optionally restricted to a system and entity):

   ```powershell
   $status = (Invoke-RestMethod "$b/systems/1/entities?version=1" | Where-Object name -eq 'Customer').fields |
     Where-Object path -eq 'status'
   Invoke-RestMethod "$b/fields/$($status.id)/similar?k=3" | ForEach-Object { $_ } |
     Select-Object system_name, entity, path, distance | Format-Table
   Invoke-RestMethod "$b/fields/$($status.id)/similar?k=3&target_system=2&target_entity=User" |
     ForEach-Object { $_ } | Select-Object entity, path, distance | Format-Table
   ```

   Expect rows ordered by ascending cosine `distance`, all from other systems.

6. Re-run the retrieval baseline (real model; it ingests the two v1 contracts into the dev
   database if they are not there yet and rewrites `docs/retrieval-baseline.md`):

   ```powershell
   cd bench
   $env:EMBEDDING_CACHE_DIR = (Resolve-Path ../.cache/fastembed).Path
   uv run --group embeddings python -m scripts.retrieval_baseline
   cd ..
   git diff --stat docs/retrieval-baseline.md
   ```

   It prints recall@1/3/5 for both scopes. The script refuses the fake provider unless
   `--test-only` is passed, and a test-only run cannot overwrite the committed report.

### Retrieval baseline result

Read `docs/retrieval-baseline.md` for the generated tables, the chance level and the
top-5 for every miss. Summary of the recorded run (2026-10-06, `BAAI/bge-small-en-v1.5`):
recall@1 is 7/8 in both scopes and recall@3 is 8/8. The one mapping not found first is
`externalRef` (DIRECT), because the same source field, `customer_id`, also feeds `userId`
and retrieval can rank only one of them first; both are in the top 2. Retrieval did place
`userId`, `tier` and `accountState` first, but that is partly because the mock contracts
describe their fields generously (see the caveats in `docs/discovery.md`), the sample is 8
mappings, and what retrieval cannot do (pick between candidates, say a mapping is
TRANSFORMATION or DERIVED, produce `C-1837` -> `1837`) is exactly what v0.3 must add.

