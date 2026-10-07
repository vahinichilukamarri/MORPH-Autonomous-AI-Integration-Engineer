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

## v0.3-mapping

The first LLM step: retrieval-augmented mapping proposals, deterministic validation, confidence
and review flags, versioned persistence, and a grader and evaluation against hand-written answer
keys on four scenarios. Design, DSL, validation rules and the confidence formula:
`docs/mapping.md`. The answer-key judgement calls: `docs/answer-key-notes.md`.

### What exists

| Area | Where |
|---|---|
| Provider layer (Groq, Ollama, replay, cache, scripted) | `backend/app/llm/` |
| Transformation DSL, agent, validation, confidence, runner, review | `backend/app/mapping/` |
| Versioned prompt templates (v1, frozen) | `backend/app/mapping/prompts/v1/` |
| Mapping tables (migration `0003`) and API | `backend/app/db_models.py`, `backend/app/api/mapping.py` |
| Sample records of the mock systems | `mock_systems/samples/` |
| Four scenarios, answer keys, reference pipelines, checksum manifest | `bench/scenarios/`, `bench/references/` |
| Grader and evaluation script | `bench/morph_bench/grader.py`, `bench/scripts/run_mapping_eval.py` |

### Run and verify without an API key (PowerShell, from the repo root)

```powershell
docker compose up -d --wait postgres
./scripts/dev.ps1 lint
./scripts/dev.ps1 test
```

All LLM behaviour is tested with fake and replay providers; CI never touches the network. The
tests create and drop their own throwaway databases.

### Run the mapping API with a real model (needs GROQ_API_KEY)

Put the key in the gitignored `.env` at the repo root (never commit it):

```powershell
Add-Content .env 'GROQ_API_KEY=<your key>'
./scripts/dev.ps1 up
./scripts/dev.ps1 ingest crm http://localhost:8101/openapi.json
./scripts/dev.ps1 ingest support http://localhost:8102/openapi.json
$b = 'http://localhost:8000'
$run = Invoke-RestMethod -Method Post "$b/mapping-runs" -ContentType 'application/json' -Body (@{
  source_system_version = 1; target_system_version = 2
  source_entity = 'Customer'; target_entity = 'User'; mode = 'rag'
  requirement = 'Customers on the enterprise segment are entitled to priority support.' } | ConvertTo-Json)
$run.summary
Invoke-RestMethod "$b/mapping-runs/$($run.id)/mappings" | ForEach-Object { $_ } |
  Select-Object target_field, @{n='type';e={$_.current.mapping_type}},
    @{n='validation';e={$_.current.validation_status}}, @{n='review';e={$_.current.review_status}},
    @{n='confidence';e={$_.current.confidence}} | Format-Table
```

(`source_system_version` and `target_system_version` are system *version ids* from
`GET /systems/{id}/versions`; adjust them if your database has other versions.) Approve or
override one mapping, then list its versions:

```powershell
Invoke-RestMethod -Method Post "$b/mappings/1/review" -ContentType 'application/json' -Body '{"action":"approve"}'
Invoke-RestMethod "$b/mappings/1/versions" | ForEach-Object { $_ } | Select-Object version, author, review_status
```

### Run the real evaluation

Needs `GROQ_API_KEY` in `.env`. The model defaults to `openai/gpt-oss-120b` (`GROQ_MODEL`). The
first run is N=1 and also records the replay fixtures for scenario 1:

```powershell
./scripts/dev.ps1 mapping-eval --n-runs 1 --record-replays crm_customer_to_support_user
```

Groq's free tier is rate limited, so this can take a long while and may stop with "stopped by a
rate limit". Every finished call is saved in `.cache/mapping-eval/`; run the **same command
again** later and it resumes without repeating finished calls. An interrupted run writes its
partial report to `.run/mapping-eval.partial.md`, never to `docs/`. When it finishes it writes
`docs/mapping-eval.md` (generated entirely from the saved results), which you review and commit.

Repeated runs to expose variance are a separate, later run that reuses the saved N=1 results:

```powershell
./scripts/dev.ps1 mapping-eval --n-runs 3
```

Re-render the report from saved results without calling the model:

```powershell
./scripts/dev.ps1 mapping-eval --report-only
```

### Re-record replays

Replays are keyed by the SHA-256 of the full prompt, so they must be re-recorded whenever a
prompt template, the retrieval context or the response schema changes. Recording only happens on
real network calls, so use a fresh store directory:

```powershell
./scripts/dev.ps1 mapping-eval --scenarios crm_customer_to_support_user --configs B,C `
  --record-replays crm_customer_to_support_user --store-dir .cache/rerecord
```

Fixtures land in `bench/replays/`. CI replays scenario 1 in `full_schema` mode (the RAG replay
depends on real embeddings and is checked locally). The replay test also compares the result with
`bench/replays/crm_customer_to_support_user.expected.json`, which holds the per-field outcome of
the recorded real run; regenerate it from the new run's saved results together with the fixtures.

### Frozen before the first real run

Prompt templates v1 and the confidence constants are frozen (a test enforces it). Any change
after the first real run is prompt v2 or confidence formula v2 and appears in
`docs/mapping-eval.md` as a separate labelled run set.

### Recorded results

The first real run (N=1, prompt v1 / confidence-v1, `openai/gpt-oss-120b` on Groq) is in
`docs/mapping-eval.md`. It is generated from the saved run results; read it there rather than
here. The scenario 1 responses of that run are the replay fixtures CI uses.
