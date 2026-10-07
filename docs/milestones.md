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

### Validator v2 rescoring (post-hoc)

Re-score the saved v1 responses with validator v2. Makes no LLM calls and leaves the v1 report
text above it byte-identical; it only appends or replaces the v2 section:

```powershell
./scripts/dev.ps1 up
cd bench
$env:EMBEDDING_CACHE_DIR = (Resolve-Path ../.cache/fastembed).Path
uv run --group embeddings python -m scripts.rescore_v2
cd ..
git diff --stat docs/mapping-eval.md
```


## v0.4-codegen

Turns reviewed mappings into a working integration that runs only in a Docker sandbox and is
graded by a hidden, hand-written oracle. Design: `docs/codegen.md` (what is generated, the
review gate, sync behaviour, known limitations), `docs/sandbox.md` (limits, threat model and the
containment evidence), `docs/oracle.md` (categories, scoring, how it stays hidden).

### What exists

| Area | Where |
|---|---|
| Trusted runtime (HTTP, retry, paging, report, sync engine) | `sandbox/runtime/morph_runtime/` |
| Sandbox image, gate configuration | `sandbox/Dockerfile`, `sandbox/gate/` |
| Compiler, operation analysis, review gate, strategy, bundle, service, API | `backend/app/codegen/`, `backend/app/api/integrations.py` |
| Static gate (AST, ruff, mypy) and sandbox runner | `backend/app/codegen/gate.py`, `gate_tools.py`, `sandbox.py` |
| L1 (strategy) and L2 (sync module) conditions, frozen prompts | `backend/app/codegen/llm_codegen.py`, `backend/app/codegen/prompts/v1/` |
| Migration `0005` (integrations, versions, files, gate results, sandbox runs) | `backend/alembic/versions/0005_integrations.py` |
| Mock admin API (state, request log, counters, deterministic faults) | `mock_systems/common/faults.py` |
| Oracle (fixtures, harness, tests), pinned in the manifest | `bench/oracle/`, `bench/morph_bench/oracle/` |
| Evaluation script and report | `bench/scripts/run_codegen_eval.py`, `docs/codegen-eval.md` |

### Run and verify (PowerShell, from the repo root; needs Docker and the dev Postgres)

```powershell
docker compose up -d --wait postgres
./scripts/dev.ps1 sandbox-build        # builds morph-sandbox:dev and morph-sbx-mock:dev (labelled)
./scripts/dev.ps1 lint
./scripts/dev.ps1 test                 # includes the runtime library tests; no Docker needed
./scripts/dev.ps1 sandbox-test         # containment corpus, gate tools, generated tests (about 5 min)
./scripts/dev.ps1 oracle               # the oracle against condition D (about 17 min)
./scripts/dev.ps1 codegen-eval --conditions D
```

Nothing here calls a model. L1 and L2 with a real model need `--confirm-real-run` and are run
only after an explicit go (Checkpoint D); CI uses scripted or replayed providers only.

### Oracle result (condition D, approved mappings)

One complete run, 413 checks, no failures. Real counts per scenario (passed/total):

| | O1 | O2 | O3 | O4 | O5 | O6 | O7 | O8 | all |
|---|---|---|---|---|---|---|---|---|---|
| S1 | 18/18 | 19/19 | 11/11 | 20/20 | 29/29 | 37/37 | 5/5 | 4/4 | 143/143 |
| S3 | 17/17 | 19/19 | 11/11 | 9/9 | 29/29 | 33/33 | 5/5 | 4/4 | 127/127 |
| S4 | 18/18 | 19/19 | 11/11 | 20/20 | 29/29 | 37/37 | 5/5 | 4/4 | 143/143 |

By revision: 301 checks from the first oracle version (r0), 14 revised after the first D run
(r1-fix) and 98 added after it (r1-new). S3 skips O4 at 100 and 250 keys by design (sizes 0, 1
and 101 are used). **The oracle was revised after D had been run against it**, so this table
is not a blind result; the revision is disclosed below. The committed evaluation report is
`docs/codegen-eval.md`.

### Oracle revision r1 (post-hoc), and what the oracle and its design found in D

After the first complete D run (5 failed tests out of 57) the oracle was revised, with every
decision made by the project owner, who asked that no expectation other than these be changed:

| Failing check | Whose fault | Oracle change (before -> after) |
|---|---|---|
| O5 `http_500_on_source` (S1, S4): "no fault seen" | oracle: the source gets one request and a 30% seeded fault never fired | probabilistic seed -> the mock fails the **first** request deterministically; injection is now proven from the mock's own fault counter for the 500 and malformed-JSON cases |
| O7 `no_request_reaches_the_target` (S1, S4) | oracle harness: a request hung by the previous test's timeout fault was logged into the next test's window (a 504 `GET` logged 33 s after it began) | `seed()` now waits until neither mock has a request in flight (counter, not request-start logging) |
| O7 S3 target drift: `at_least_one_record_failed`, `run_does_not_crash` | oracle over-specified (and D had a bug, below) | replaced by: ends `CONTRACT_DRIFT`, exit code 2, and no write beyond the records reported before the drift; the "each record fully old or fully new" check is unchanged |

Also fixed after the fact: a missing S3 fixture quote (YAML syntax, no value changed) and a
lookup-table omission in my own r1 edit that made the 429 test error before checking anything.
Added after the first D run (`r1-new`): an unknown enum value from the source fails only that
record; the same source key twice in one run writes once; a created_at that is not an ISO
string or not a string fails that record (for S3, where created_at is not writable and so not
read, the record must sync normally). Expected values are hand-written in the fixtures.

D bugs found and fixed, and **how each was found** (the oracle itself caught one of them):

1. **Identity semantics, found by reviewing the fixtures against the answer key** (before any
   oracle run). The sync engine created CRM customers for users without `externalRef` by matching
   on email, which the reviewed answer key forbids (an id is never invented). Fixed: a mapped
   target identity that is null is `NOT_SYNCABLE`; an id the CRM does not have cannot be created.
2. **A missing source identity field reported as "no id" instead of contract drift, found by a
   runtime unit test** (before the oracle existed). Fixed: absent is drift, null is
   `NOT_SYNCABLE`.
3. **A missing required target field treated as null, which half-applied a record** (new name and
   email, old phone, under CRM v2). **The only one caught by a failing oracle run** (O7, S3).
   Fixed in the runtime and the generator: required response fields are derived from discovery
   and an absent one is `CONTRACT_DRIFT` before any write.
4. **The bundle mount was unreadable to the sandbox user on a Linux host, found by CI** (the
   limits verification refused to run anything). Fixed: bundles are made world-readable and never
   writable, and a test checks that no bundle holds a secret or environment value.

The oracle's design also forced the other changes recorded here (time bounds that guarantee a
clean end inside the sandbox limit, the `KEYS` mode for a source that cannot be listed).

### Frozen before the first real run

Prompt templates v1 and the confidence constants are frozen (a test enforces it). Any change
after the first real run is prompt v2 or confidence formula v2 and appears in
`docs/mapping-eval.md` as a separate labelled run set.

### Recorded results

The first real run (N=1, prompt v1 / confidence-v1, `openai/gpt-oss-120b` on Groq) is in
`docs/mapping-eval.md`. It is generated from the saved run results; read it there rather than
here. The scenario 1 responses of that run are the replay fixtures CI uses.

### Validator v2 rescoring (post-hoc)

Re-score the saved v1 responses with validator v2. Makes no LLM calls and leaves the v1 report
text above it byte-identical; it only appends or replaces the v2 section:

```powershell
./scripts/dev.ps1 up
cd bench
$env:EMBEDDING_CACHE_DIR = (Resolve-Path ../.cache/fastembed).Path
uv run --group embeddings python -m scripts.rescore_v2
cd ..
git diff --stat docs/mapping-eval.md
```


## v0.4-codegen

Turns reviewed mappings into a working integration that runs only in a Docker sandbox and is
graded by a hidden, hand-written oracle. Design: `docs/codegen.md` (what is generated, the
review gate, sync behaviour, known limitations), `docs/sandbox.md` (limits, threat model and the
containment evidence), `docs/oracle.md` (categories, scoring, how it stays hidden).

### What exists

| Area | Where |
|---|---|
| Trusted runtime (HTTP, retry, paging, report, sync engine) | `sandbox/runtime/morph_runtime/` |
| Sandbox image, gate configuration | `sandbox/Dockerfile`, `sandbox/gate/` |
| Compiler, operation analysis, review gate, strategy, bundle, service, API | `backend/app/codegen/`, `backend/app/api/integrations.py` |
| Static gate (AST, ruff, mypy) and sandbox runner | `backend/app/codegen/gate.py`, `gate_tools.py`, `sandbox.py` |
| L1 (strategy) and L2 (sync module) conditions, frozen prompts | `backend/app/codegen/llm_codegen.py`, `backend/app/codegen/prompts/v1/` |
| Migration `0005` (integrations, versions, files, gate results, sandbox runs) | `backend/alembic/versions/0005_integrations.py` |
| Mock admin API (state, request log, counters, deterministic faults) | `mock_systems/common/faults.py` |
| Oracle (fixtures, harness, tests), pinned in the manifest | `bench/oracle/`, `bench/morph_bench/oracle/` |
| Evaluation script and report | `bench/scripts/run_codegen_eval.py`, `docs/codegen-eval.md` |

### Run and verify (PowerShell, from the repo root; needs Docker and the dev Postgres)

```powershell
docker compose up -d --wait postgres
./scripts/dev.ps1 sandbox-build        # builds morph-sandbox:dev and morph-sbx-mock:dev (labelled)
./scripts/dev.ps1 lint
./scripts/dev.ps1 test                 # includes the runtime library tests; no Docker needed
./scripts/dev.ps1 sandbox-test         # containment corpus, gate tools, generated tests (about 5 min)
./scripts/dev.ps1 oracle               # the oracle against condition D (about 17 min)
./scripts/dev.ps1 codegen-eval --conditions D
```

Nothing here calls a model. L1 and L2 with a real model need `--confirm-real-run` and are run
only after an explicit go (Checkpoint D); CI uses scripted or replayed providers only.

### Oracle result (condition D, approved mappings)

One complete run, 413 checks, no failures. Real counts per scenario (passed/total):

| | O1 | O2 | O3 | O4 | O5 | O6 | O7 | O8 | all |
|---|---|---|---|---|---|---|---|---|---|
| S1 | 18/18 | 19/19 | 11/11 | 20/20 | 29/29 | 37/37 | 5/5 | 4/4 | 143/143 |
| S3 | 17/17 | 19/19 | 11/11 | 9/9 | 29/29 | 33/33 | 5/5 | 4/4 | 127/127 |
| S4 | 18/18 | 19/19 | 11/11 | 20/20 | 29/29 | 37/37 | 5/5 | 4/4 | 143/143 |

By revision: 301 checks from the first oracle version (r0), 14 revised after the first D run
(r1-fix) and 98 added after it (r1-new). S3 skips O4 at 100 and 250 keys by design (sizes 0, 1
and 101 are used). **The oracle was revised after D had been run against it**, so this table
is not a blind result; the revision is disclosed below. The committed evaluation report is
`docs/codegen-eval.md`.

### Oracle revision r1 (post-hoc), and what the oracle and its design found in D

After the first complete D run (5 failed tests out of 57) the oracle was revised, with every
decision made by the project owner, who asked that no expectation other than these be changed:

| Failing check | Whose fault | Oracle change (before -> after) |
|---|---|---|
| O5 `http_500_on_source` (S1, S4): "no fault seen" | oracle: the source gets one request and a 30% seeded fault never fired | probabilistic seed -> the mock fails the **first** request deterministically; injection is now proven from the mock's own fault counter for the 500 and malformed-JSON cases |
| O7 `no_request_reaches_the_target` (S1, S4) | oracle harness: a request hung by the previous test's timeout fault was logged into the next test's window (a 504 `GET` logged 33 s after it began) | `seed()` now waits until neither mock has a request in flight (counter, not request-start logging) |
| O7 S3 target drift: `at_least_one_record_failed`, `run_does_not_crash` | oracle over-specified (and D had a bug, below) | replaced by: ends `CONTRACT_DRIFT`, exit code 2, and no write beyond the records reported before the drift; the "each record fully old or fully new" check is unchanged |

Also fixed after the fact: a missing S3 fixture quote (YAML syntax, no value changed) and a
lookup-table omission in my own r1 edit that made the 429 test error before checking anything.
Added after the first D run (`r1-new`): an unknown enum value from the source fails only that
record; the same source key twice in one run writes once; a created_at that is not an ISO
string or not a string fails that record (for S3, where created_at is not writable and so not
read, the record must sync normally). Expected values are hand-written in the fixtures.

D bugs found and fixed, stated precisely about how each was found:

1. **Identity semantics (found while writing the S3 fixture, before any oracle run).** The sync
   engine created CRM customers for users without `externalRef` by matching on email, which the
   reviewed answer key forbids (an id is never invented). Fixed: a mapped target identity that is
   null is `NOT_SYNCABLE`; an id the CRM does not have cannot be created.
2. **A missing source identity field reported as "no id" instead of contract drift** (found by a
   runtime unit test before the oracle existed). Fixed: absent is drift, null is `NOT_SYNCABLE`.
3. **A missing required target field treated as null, which half-applied a record** (new name and
   email, old phone, under CRM v2). **Caught by a failing oracle run** (O7, S3). Fixed in the
   runtime and the generator: required response fields are derived from discovery and an absent
   one is `CONTRACT_DRIFT` before any write.

Only the third was caught by the oracle failing; the other two were found earlier. The oracle's
design also forced the other changes recorded here (time bounds that guarantee a clean end inside
the sandbox limit, the `KEYS` mode for a source that cannot be listed).

### Frozen before the first real run

Codegen prompts `codegen-v1` (four files) and the rendered S1 L1 and L2 prompts have golden
hashes in `backend/tests/codegen/test_frozen_codegen.py`, as do the generator, runtime, compiler
and gate versions and the condition D bundle hashes for S1 and S3. If one of those tests fails,
introduce `v2` and report it as a separate, labelled run; do not retune on the same scenarios.

### Known limitations

See `docs/codegen.md`. Notably: **S3 is update-only** (ids are never invented and the CRM cannot
create under a chosen id), which supersedes the plan's natural-key creation for S3; Support
cannot be listed, so a Support source needs operator-supplied ids.

### Real-model run of L1 and L2 (Groq `openai/gpt-oss-120b`, N = 1 per unit)

Real counts, full analysis in `docs/codegen-eval-findings.md`, generated tables in
`docs/codegen-eval.md`. On the approved inputs of S1, S3 and S4: **D reached a ready,
oracle-correct integration in 3 of 3; L1 in 0 of 3** (two proposals invalid after the one
re-ask, one rejected by the AST gate) **and L2 in 0 of 3** (all rejected by the AST stage, so
nothing reached ruff, mypy, the sandbox or the oracle). The six as-proposed units blocked before
any model call (zero calls). Nine model calls in total, 38,224 input and 11,285 output tokens
(2,754 of them reasoning), no HTTP 400, no rate-limit stop. Part of the failures trace to our own
frozen design (the L2 prompt told the model to use `error.__cause__`, which the gate bans; the
L1 `omit_if_null` rule; the gate's long-identifier rule flagged benign test data); fixing them
would be a separately labelled `codegen-v2` run. The real replies are committed in
`bench/replays/codegen/` and replayed in CI with no network.
