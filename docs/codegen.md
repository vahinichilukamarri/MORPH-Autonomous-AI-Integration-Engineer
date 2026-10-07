# Code generation (v0.4)

Turns reviewed mappings into a working integration that runs only in the sandbox
([sandbox.md](sandbox.md)) and is graded by the hand-written oracle ([oracle.md](oracle.md)).

## What is generated, and what is not

| Piece | Produced by | LLM |
|---|---|---|
| `integration/transform.py`: one function per target field, `to_target(record)` | deterministic compiler from the approved DSL pipelines (`app/codegen/compiler.py`) | no |
| `integration/clients.py`: auth scheme per system, URLs and credentials from `MORPH_*` environment variables | deterministic, from the discovered security schemes | no |
| `integration/strategy.py`: which operations to call, identity field, writable fields, pagination | deterministic, from the discovered operations (`operations.py`, `generator.py`) | no (condition D) |
| `integration/__main__.py` | fixed template | no |
| `tests_generated/`: the DSL interpreter's result for every sample record, checked against the compiled code | deterministic (`generated_tests.py`) | no |
| `morph_runtime` (HTTP client, retry/backoff, pagination, sync engine, report, exit codes) | hand-written, tested, installed in the sandbox image | no |

Everything that can be produced deterministically is. The model-assisted conditions (L1: a
validated sync strategy and extra tests, L2: a free-form sync module) are built last and run only
on an explicit go; whether they add anything over D is a measured question, not an assumption.

## The compiler

Every DSL op is one call into `morph_runtime.ops` with literal arguments, so generated code is a
flat list of calls the static gate can check. A Hypothesis differential test runs random pipelines
and records through both the DSL executor and the compiled code and requires identical results,
including the same error class. Breaking an op by hand makes it fail.

## The review gate

Per target field, using the latest mapping version:

* accepted (`AUTO_ACCEPTED`, `APPROVED`, `OVERRIDDEN`) and not `UNRESOLVED`: compiled;
* anything else: excluded and recorded in the manifest with the reason;
* an excluded field that is **required and writable** blocks the whole integration
  (`BLOCKED_PENDING_REVIEW`, no code, the blocking fields listed);
* excluded optional fields also block unless the operator passes `allow_partial` (then the
  manifest says exactly what is not synced);
* fields the target API cannot write (for the CRM: `customer_id`, `created_at`) are never sent and
  are listed as `not_writable`.

Approving or overriding a mapping appends a mapping version, and the next generation produces a
new integration version. Bundles are immutable and content-addressed (`input_hash`,
`bundle_hash`); regenerating with unchanged inputs returns the existing version.

## Sync behaviour (the runtime engine)

* One outcome per source record: `CREATED`, `UPDATED`, `UNCHANGED`, `NOT_SYNCABLE`, `FAILED`
  (with a category, and the field names for validation errors).
* The target record is looked up first; nothing is written when it already holds the mapped
  values, so a second run issues no write requests.
* Auth failures stop the run after one attempt. Retries (429 honouring `Retry-After`, 5xx,
  timeouts, malformed JSON) are bounded per request and per run; a `POST` is not retried after an
  ambiguous failure, the record is looked up first.
* A source field the mapping needs being absent, or a required field absent from a target record,
  is contract drift: the run stops before writing that record. Absent is not null.
* Constants (a mapped field with no source) are sent when creating a record and never overwrite
  an existing one.

### Direction-specific facts found while building this

* **Support has no list operation.** For Support as the source (S3) there is nothing to
  enumerate, so the strategy has a `KEYS` mode: the operator supplies the ids
  (`MORPH_SOURCE_KEYS`) and each record is read by id.
* **The CRM assigns its own ids and has no upsert.** Following the reviewed answer key, a user
  without `externalRef` is not syncable (an id is never invented), and a user whose `externalRef`
  does not exist in the CRM is not syncable either, because a customer cannot be created under a
  chosen id. S3 therefore only updates existing customers. A natural-key (email) lookup is used
  only when no target identity is mapped at all.
* **`segment` is unresolved in the S3 answer key.** Without a human decision generation is
  blocked; with the recorded test-only constant `SMB` it is generated, and the constant is
  create-only, so it never overwrites an existing customer's segment.

## API

`POST /integrations` (`mapping_run_id`, `condition`, `allow_partial`), `GET /integrations/{id}`,
`.../versions`, `.../versions/{n}`, `.../files`, `.../gate`, `.../sandbox-runs`, and
`POST .../versions/{n}/run-tests` (the generated tests, in the sandbox, reported separately from
the oracle).
