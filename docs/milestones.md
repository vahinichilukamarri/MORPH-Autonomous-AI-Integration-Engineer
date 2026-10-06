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
