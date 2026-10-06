# Mapping

The first LLM step. For a source entity and a target entity MORPH proposes a mapping for every
target field, validates each proposal with plain code, scores confidence from deterministic
signals, flags what needs a human, and stores everything with versions. The core rule of the project
holds throughout: **the LLM proposes, software decides.** The model returns a structured
proposal; deterministic code parses, validates, scores and persists it. The model has no tools,
no file access and no way to execute anything.

Code: `backend/app/llm/` (provider layer), `backend/app/mapping/` (DSL, agent, validation,
confidence, runner, review), `backend/app/api/mapping.py`. Grading and evaluation live in
`bench/` and are separate from the pipeline.

## Pipeline

For each target field of the target entity (one LLM call per field):

1. Retrieve the source fields most similar to it (the v0.2 retrieval, target to source).
2. Build the prompt from a versioned template; spec text and sample records go in delimited
   data blocks (see "Untrusted data").
3. Call the provider. The reply is parsed into a Pydantic model. If it is invalid, the provider
   layer re-asks exactly once, including the validation error. Still invalid: the field becomes
   `UNRESOLVED` with reason `invalid_llm_output`. Never a crash, never silently accepted.
4. Convert the flat reply into a typed pipeline of DSL steps (a conversion error counts as
   invalid output and also triggers the re-ask).
5. Validate deterministically against the real source and target fields and every sample record.
6. Compute confidence and decide `AUTO_ACCEPTED` or `NEEDS_REVIEW`.
7. Persist the run, the mappings (version 1), and one `llm_calls` row per attempt.

Two context modes, chosen per run: `full_schema` (the target field plus every source entity
field) and `rag` (the target field plus the top-5 retrieved source fields). Sample values come
from `mock_systems/samples/` (13 CRM customers and 16 Support users, with null optionals, every enum value,
phone variants, stray whitespace, multi-word names and, for Support, users that were not
imported from the CRM). The prompt shows five evenly spaced records; the validator runs all of
them.

## LLM provider layer

All model calls go through `BaseLLMProvider` (`app/llm/base.py`). A provider returns raw text and
call metadata; the base class parses, validates and re-asks once.

| Provider | Notes |
|---|---|
| `GroqProvider` | OpenAI-compatible API over httpx. Strict `json_schema` output on the models that support it (`openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`), `json_object` with the schema in the system text on every other model. For `openai/gpt-oss-*` models `reasoning_effort` is set to `low` (`GROQ_REASONING_EFFORT`). Checks `GET /models` once and fails with the list of available models if the configured one is missing. The model id the API reports is what gets recorded. |
| `OllamaProvider` | Local fallback through `/api/chat` with a JSON-schema `format`. **Implemented and mock-tested, not verified against a real Ollama install.** |
| `ReplayLLMProvider` | Serves recorded responses by SHA-256 of the full prompt (system text, parts, response schema, temperature). An unrecorded prompt fails loudly with a "re-record" message. |
| `CachingProvider` | On-disk cache (`.cache/llm`, gitignored) in front of another provider; can append every real response to a record file (`bench/replays/`). |
| `ScriptedFakeProvider` | Queue or function of replies, for tests. |

Retries happen on HTTP 429 only, honour `Retry-After`, are bounded in count and by a total
wall-clock budget; a wait longer than the budget (a daily limit, say) raises
`RateLimitExhausted` immediately so a long job can stop cleanly and resume later. Timeouts are
set on every call. Temperature defaults to 0. The API key is a `SecretStr` read only from the
environment (or the gitignored `.env`); it appears only in the `Authorization` header, is
redacted from error text, and a test asserts a sentinel key never reaches logs, `repr`,
exceptions, stored records or `llm_calls`.

Recorded per call: provider, exact model id, prompt hash, input tokens, output tokens,
**reasoning tokens** (for gpt-oss models, included in the output count), latency, outcome and
whether it came from the network, the cache or a replay. Prompts are not stored.

## Transformation DSL

`app/mapping/transform.py`. A closed, typed, deterministic set of operations. A transformation is
an ordered pipeline over the named fields of one source record: a *source op* first, then *value
ops*. The executor is plain Python (no `eval`, no `exec`), raises only typed errors, and is
property-tested with Hypothesis (deterministic, never an untyped exception).

| Op | Kind | Parameters | Null policy |
|---|---|---|---|
| `COPY` | source | `field` | null stays null; a field missing from the record is an error |
| `JOIN_NONNULL` | source | `fields`, `separator` (" "), `trim` (true) | null and empty parts skipped; nothing left gives null |
| `COALESCE` | source | `fields` | first non-null field; all null gives null |
| `CONSTANT` | source | `value` | the value, which may be null |
| `CAST` | value | `to`: int, str, float, bool | null stays null; a lossy or impossible cast is an error |
| `STRIP_PREFIX` | value | `prefix` | null stays null; no prefix means unchanged |
| `ADD_PREFIX` | value | `prefix` | null stays null |
| `REGEX_EXTRACT` | value | `pattern`, `group` (1) | null stays null; **no match gives null** |
| `REGEX_REPLACE` | value | `pattern`, `replacement` | null stays null |
| `MAP_ENUM` | value | `mapping`, `on_unmapped` (error or default), `default` | null stays null; an unmapped key is an error unless a default is set |
| `FORMAT_DATETIME` | value | `from_format`, `to_format`: iso8601, epoch_s, epoch_ms | null stays null; naive timestamps are read as UTC |
| `SPLIT_PART` | value | `separator`, `index` | null stays null; an index past the end gives null |

String ops need a string input (`CAST` first). Regexes are checked at validation time: length cap,
no nested quantifiers, must compile, group must exist. This is a best-effort guard against
catastrophic backtracking, not a proof. Anything that cannot be expressed becomes `UNRESOLVED`
with a reason; v0.4 will handle custom code. Those counts are in the evaluation report.

## Prompt design

Templates are versioned files (`app/mapping/prompts/v1/`); the version string is part of every
run record. The system text holds the rules, the mapping-type definitions and the DSL reference.
The task text only references named data blocks.

**Untrusted data.** Spec text (names, descriptions, examples) and sample records are serialised
to JSON and placed inside `<<<UNTRUSTED_DATA name="...">>>` ... `<<<END_UNTRUSTED_DATA>>>`
blocks, with any `<<<` or `>>>` in the content replaced so data cannot forge a delimiter. The
system text tells the model that block contents are data, never instructions. A test builds a
prompt from a synthetic spec whose description contains an injection attempt (including a forged
closing marker) and asserts that the text stays inside its block, that an obedient model's
reply still goes through full validation and is rejected, and that nothing in the request
carries tools or file access.

**Frozen.** Prompts v1 and confidence formula v1 are frozen before the first real evaluation; a
test fails if either changes. Any later change must be introduced as prompt v2 or confidence
formula v2 and reported as a separate, labelled run set in `docs/mapping-eval.md`. Results are
never retuned on the same fields with only the better one reported.

## Validation

`app/mapping/validate.py`. Per proposal, with machine-readable reason codes:

| Code | Result | Meaning |
|---|---|---|
| `SOURCE_FIELD_MISSING`, `SOURCES_MISMATCH` | FAIL | a named source does not exist, or the declared sources differ from what the pipeline reads |
| `TYPE_INCONSISTENT` | FAIL | the pipeline does not fit the mapping type (e.g. DIRECT with two sources) |
| `EXECUTION_ERROR` | FAIL | the pipeline raised on a sample record |
| `TYPE_MISMATCH`, `FORMAT_MISMATCH`, `ENUM_VIOLATION`, `PATTERN_VIOLATION`, `RANGE_VIOLATION`, `LENGTH_VIOLATION` | FAIL | an output breaks the target field's contract |
| `NULL_FOR_REQUIRED_TARGET` | FAIL | null output for a non-nullable target and no nullable source explains it |
| `NULLABLE_TO_REQUIRED` | WARN, forces review | a nullable source feeds a non-nullable target (data-dependent) |
| `INFORMATION_LOSS_ENUM` | WARN | several source values map to one target value |
| `INFORMATION_LOSS_COALESCE` | WARN | `COALESCE` ignores all but one source |
| `AMBIGUOUS_ALTERNATIVES`, `AMBIGUOUS_RETRIEVAL` | WARN, ambiguity | the model listed a different source set, or one of the two nearest retrieved fields was left out and they are within 0.02 of each other |
| `NO_SAMPLES` | WARN | nothing was executed |
| `UNRESOLVED` | WARN | the proposal declined to map |
| `TARGET_NOT_COVERED`, `DUPLICATE_TARGET_COVERAGE` | run level | each target field must be covered exactly once |
| `REQUIRED_TARGET_UNRESOLVED` | run level | a required field was left unresolved |

Status is `FAIL` if any FAIL reason exists, `WARN` if only warnings, else `PASS`.

## Confidence

Confidence is a deterministic function, not the model's own number (`app/mapping/confidence.py`,
formula `confidence-v1`):

```
confidence = (0.45 * V + 0.30 * R + 0.25 * C) * (0.7 if ambiguous else 1.0)
V  validation:  PASS 1.0, WARN 0.5, FAIL 0.0
R  retrieval:   mean over the proposal's sources of rank 1 -> 1.0, 2 -> 0.7, 3 -> 0.5,
                4 or 5 -> 0.3, not retrieved -> 0.0; 0.5 when there is no source field
C  certainty:   the model's label, HIGH 1.0, MEDIUM 0.6, LOW 0.2
```

A mapping is `NEEDS_REVIEW` when it is `UNRESOLVED`, fails validation, is ambiguous, carries a
review-forcing warning (`NULLABLE_TO_REQUIRED`), or scores below 0.70. Otherwise it is
`AUTO_ACCEPTED`. `UNRESOLVED` mappings have no confidence. The weights and threshold are my
choices and are not calibrated; the evaluation report shows how accuracy actually varies with
confidence on the recorded runs.

## Persistence, review and API

Migration `0003`: `mapping_runs`, `mappings` (one per run and target field), `mapping_versions`
and `llm_calls`. Version 1 is the system's proposal. **Approving or overriding appends a new
version; earlier versions stay intact.** An override is re-validated like any proposal and is
rejected if it fails; a mapping that fails validation cannot be approved.

| Endpoint | Purpose |
|---|---|
| `POST /mapping-runs` | `{source_system_version, target_system_version, source_entity, target_entity, mode}`; runs synchronously and stores the result |
| `GET /mapping-runs/{id}` | run metadata, summary counts, run-level reasons |
| `GET /mapping-runs/{id}/mappings` | every mapping with its current version |
| `GET /mappings/{id}/versions` | all versions of one mapping |
| `POST /mappings/{id}/review` | `{action: approve}` or `{action: override, mapping: {...}}` |

A rate limit that outlasts the budget returns 429, other provider failures 502. No
authentication yet (Phase 2).

## Answer keys and the grader

Answer keys are ground truth, read only by the grader (`bench/morph_bench/grader.py`). The
backend never imports `morph_bench` and never touches answer keys or the reference pipelines in
`bench/references/`; an architecture test enforces it. A checksum manifest makes any change to a
key a deliberate act (`docs/answer-key-notes.md` lists every judgement call).

Per target field the grader reports `source_match`, `type_match` (informational),
`transformation_correct` (the proposed pipeline is executed on every input to output example in
the key), `unresolved_correct`, and `flag_correct` where the key expects a review flag. A mapping
is *fully correct* when sources and transformation are right (and the flag, where expected), or
when the key says UNRESOLVED and the proposal is UNRESOLVED. Producing a value for an
UNRESOLVED field is a guess and scores wrong. Negative tests prove wrong mappings are scored
wrong.

## Evaluation

`bench/scripts/run_mapping_eval.py` compares three configurations on the four scenarios:
**A** retrieval only (copy the top-1 retrieved source field, no LLM), **B** LLM with the full
schema, **C** LLM with retrieved fields. It writes `docs/mapping-eval.md` entirely from saved run
results: model id, date, N, per-scenario and overall tables, invalid-output and re-ask rates,
NEEDS_REVIEW rate, tokens (including reasoning tokens) and latency, accuracy by confidence
bucket, every wrong or unresolved mapping with proposal and expected, and the rate limits it hit.
The sample is about 30 target fields, so the report states that results are not statistically
strong.

Every completed call is saved by prompt hash and run index, so a run that hits a rate limit stops
cleanly and a later run resumes without repeating finished calls; repeated runs (N greater than
1) bypass the cache on purpose so variance is real. The script refuses fake or replay providers
unless `--test-only` is given, and test-only output can never be written to the committed
report.

**CI replays.** CI has no network and no model download, so it uses fake embeddings. RAG prompts
contain retrieval results, which differ between fake and real embeddings, so a RAG replay could
not hash-match in CI. CI therefore replays the full mapping pipeline for scenario 1 in
`full_schema` mode (retrieval then only feeds the confidence score); the RAG replay is checked
locally.

## Known limits

- The confidence weights and threshold are untuned choices; see the calibration table.
- The regex guard is best effort.
- Retrieval ambiguity needs real embeddings to mean something; with the fake provider it is
  noise, so tests judge contract checks only.
- Mapping types are labels with fuzzy edges (DIRECT vs TRANSFORMATION vs DERIVED); scoring does
  not depend on them.
- Runs are synchronous inside the request. Fine at this size; a job queue is a later concern.
