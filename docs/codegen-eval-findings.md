# Code generation evaluation: findings (v0.4 real run)

Companion to the generated report [codegen-eval.md](codegen-eval.md), whose numbers all come
from saved results. This page explains them. **N is tiny** (3 approved units per condition);
nothing here is statistically significant.

## What was run

* Model: Groq `openai/gpt-oss-120b`, low reasoning effort, temperature 0, at most 4,000 output
  tokens per call; the same model and settings as the v0.3 mapping evaluation, never switched.
* Scope: L1 and L2 on the approved inputs of S1, S3 and S4 (6 units), plus the as-proposed
  inputs of all three scenarios for both conditions (6 units). **The six as-proposed units cost
  zero model calls**: v0.3 left required fields in review (`accountState`, `tier` or
  `serviceTier`, and the unresolved `segment`), so generation was blocked before any call, which
  is what a person would see.
* Frozen: the four `codegen-v1` prompt files and the rendered prompts, the generator, runtime,
  compiler and gate versions and the D bundle hashes had golden hashes in
  `backend/tests/codegen/test_frozen_codegen.py` before the first call, and nothing changed
  between the first call and the end of the run. There was no HTTP 400 from the provider, no
  schema fix, and no rate-limit stop.
* The oracle was **revised post-hoc (r1)** after D had first been run against it (see
  `milestones.md`); the D results here are from the revised oracle and are not blind. L1 and L2
  were never graded by the oracle, because none of them produced code that passed the gate.

## Model use (real counts)

| Condition | Calls | Re-asks | Input tokens | Output tokens | Reasoning tokens | Model latency |
|---|---|---|---|---|---|---|
| L1 (3 units) | 6 | 3 | 26,263 | 6,127 | 1,939 | 17,433 ms |
| L2 (3 units) | 3 | 0 | 11,961 | 5,158 | 815 | 19,422 ms |
| Total | 9 | 3 | 38,224 | 11,285 | 2,754 | 36,855 ms |

Rate-limit headers after the last call: 8,000 tokens per minute and 1,000 requests per day
limits; 991 requests and 3,069 tokens remaining in their windows. Nine requests were used. The
pacing logic never had to wait and no limit was hit. The key is read from the environment and
appears nowhere in the stored replies or the replay fixtures (a test checks this).

## L1: what the model proposed against what D's heuristic chose

| Unit | Outcome | What happened |
|---|---|---|
| S1 | `LLM_INVALID` | Same two mistakes in both attempts: `omit_if_null_fields` listed `externalRef` and `phoneNumber`, which are nullable, so they were rejected. The source (`LIST`, page parameters) and target (`UPSERT` by `PUT /users/{userId}`) were the same as D's. D's `omit_if_null` is empty. |
| S4 | `LLM_INVALID` | Same `omit_if_null_fields` mistake. The re-ask made it worse: it switched to `CREATE_UPDATE` with a `PATCH /users/{userId}` that does not exist in the contract. |
| S3 | `GATE_FAILED` | First proposal invalid (`omit_if_null` on nullable fields); the re-ask was valid and agreed with D on `KEYS` mode, `CREATE_UPDATE`, `PATCH`, and target-assigned ids. It differed on three things: it sent `segment` on **update** (`create_only` empty) where D never overwrites an existing segment, left `omit_if_null` empty where D has `status`, and used no natural key where D has `email`. By reading the proposal (the oracle never ran on it), the `segment` choice would overwrite every updated customer's segment with the constant. |

The valid S3 proposal then failed the **AST gate**: one of the four edge-case source records the
model proposed contained `externalRef = "REF-VERY-LONG-IDENTIFIER-EXCEEDING-NORMAL"` (41
characters), and the gate's opaque-string rule (`SECRET_LITERAL`, 40 or more token characters)
rejected the generated test data that carries it. That is a false positive of the gate on benign
test input, found by this run; the gate is frozen and was not changed.

## L2: what the model's `sync.py` did, and what the gate caught

All three modules were written in the expected shape (a `run(keys)` using `paginate`,
`to_target`, `RunReport`) and all three were stopped by the **AST stage**; ruff, mypy, the
sandbox and the oracle never ran on them.

| Unit | Gate finding | Cause |
|---|---|---|
| S1 | `DUNDER_ACCESS` (line 83) and `SUPPRESSION` (line 97) | The code used `fte.__cause__` to detect a missing source field, and a `# pragma: no cover` comment. **The first is exactly what our own L2 prompt's runtime reference told the model to do** (`check error.__cause__`), while the gate bans dunder attributes: a contradiction in the frozen prompt and gate, found by this run. |
| S4 | `SYNTAX` (line 177) | A stray closing `}` at the end of the module. |
| S3 | `SYNTAX` (line 145) | The same stray closing `}`. |

No banned import, `eval`, `exec`, subprocess, URL or secret was attempted. No wrong-but-plausible
sync behaviour could be caught by the oracle, because no L2 code ran.

## Honest comparison

On approved mappings, condition D produced a ready, gate-passing, oracle-correct integration in
**3 of 3** scenarios. L1 produced **0 of 3** (two invalid proposals after the one re-ask, one
gate rejection) and L2 **0 of 3** (all rejected by the AST gate). On this data D is simply
better: the model-assisted conditions added cost (9 calls, about 49,500 tokens) and no
integration. The data do not show that a model cannot do this; they show that this model, with
these frozen prompts, this validator and this gate, did not. Part of the failures trace to our
own design rather than to the model: the L2 prompt told the model to use a construct the gate
bans, the L1 validator and prompt disagreed about what `omit_if_null` means, and the gate's
secret rule flags harmless long identifiers. Fixing any of those is a prompt, validator or gate
change and therefore a new, separately labelled run (`codegen-v2`), not a retune of this one.
"D is as good as, or better than, L1 and L2" is the result.

## Corrections and caveats

* **Bug attribution.** The oracle itself caught **one** D bug (a required target field that was
  absent was treated as null, half-applying a record). The identity-semantics bug was found by
  reviewing the fixtures against the answer key, the source-identity drift bug by a runtime unit
  test, and the sandbox bundle-permission bug on Linux by CI.
* **Oracle revision r1** was made after the first D run; every change is listed in
  `milestones.md`.
* As-proposed units are rebuilt from saved v0.3 responses (no model call), validator v1.
* The model label in the saved results was corrected from `groq` to the model id
  `openai/gpt-oss-120b` (present in every recorded reply) before the report was generated.
