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

**Token accounting.** `Output tokens` is the provider's `completion_tokens` and `Reasoning tokens`
its `completion_tokens_details.reasoning_tokens`; the harness neither adds nor subtracts. Whether
`completion_tokens` already contains the reasoning tokens is **not verified**: the saved results
keep no `total_tokens` field to check it against. Under the usual OpenAI-compatible convention it
does contain them. Totals both ways: input 38,224 + output 11,285 = **49,509** tokens if reasoning
is included in output, and 49,509 + 2,754 = **52,263** if it is not. The "about 49,500" used
below is the first reading; the second is 5.6% higher. Neither changes any conclusion.

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
| S4 | `SYNTAX` (line 177) | A stray closing `}` at the end of the module, **written by the model** (see below). |
| S3 | `SYNTAX` (line 145) | The same stray closing `}`, also written by the model. |

**The stray `}` is the model's, not our extraction.** The raw reply is a JSON object with a
`source` string. In the S4 and S3 replies the *value of that string itself* ends with a `}`
line; the JSON around it is well formed and `json.loads` returns the brace as part of the code.
Our extraction does no more than read the `source` field, and the S1 reply, parsed by the same
code, ends cleanly. Last five lines of the model's code string (decoded), S4 then S3:

```text
        "source": src_client.requests_made,
        "target": tgt_client.requests_made,
    }
    return report
}
```

```text
        "source": src.requests_made,
        "target": tgt.requests_made,
    }
    return report
}
```

The raw tail of the S4 reply, as stored, is `...return report
}"` followed by the closing
`}` of the JSON object: the brace sits inside the quoted code string. The fixture is
`bench/replays/codegen/calls.jsonl` (records 6 and 9). Failure attributed to the model.

No banned import, `eval`, `exec`, subprocess, URL or secret was attempted. No wrong-but-plausible
sync behaviour could be caught by the oracle, because no L2 code ran.

## Honest comparison

On approved mappings, condition D produced a ready, gate-passing, oracle-correct integration in
**3 of 3** scenarios. L1 produced **0 of 3** (two invalid proposals after the one re-ask, one
gate rejection) and L2 **0 of 3** (all rejected by the AST gate). On this data D is simply
better: the model-assisted conditions added cost (9 calls, 49,509 to 52,263 tokens depending on the reasoning accounting above) and no
integration. The data do not show that a model cannot do this; they show that this model, with
these frozen prompts, this validator and this gate, did not. Part of the failures trace to our
own design rather than to the model: the L2 prompt told the model to use a construct the gate
bans, the L1 `omit_if_null` rule (nullable fields rejected) is a design choice that is debatable, and the gate's
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
* **Saved results were edited after the run (model label).** The first saved results recorded
  the model as `groq` for the L1 and L2 rows. That came from the harness, which read the
  model name off the provider wrapper that has no such attribute, not from the replies. I edited
  `bench/.cache/codegen-eval/real/results.jsonl` by hand: **old value `groq`, new value
  `openai/gpt-oss-120b`**, in the `model` field of the L1 and L2 rows. **Correction (2026-10-08):** the file holds 12 such
  rows (6 approved and 6 as-proposed), all carrying the new value; an earlier version of this note
  said six. The pre-edit file was not kept, so which rows read `groq` before the edit cannot be
  re-checked. The committed copy and its declared edit are in
  `bench/results/codegen-v0.4/provenance.json`. Evidence: the `model` field of
  all 9 recorded replies is `openai/gpt-oss-120b` (the Groq response's own `model`, in
  `bench/replays/codegen/calls.jsonl`, checked by a test). The results file is git-ignored, so the
  edit itself has no commit; its file time is 2026-10-07 21:51 (local). The commits that first
  published its effect are `e3b13b6` (harness label code, which then read the configured model
  rather than the reply) and `7bfdd39` (the report and these findings). **Fix (this revision):**
  `UnitResult.response_model` is now filled from the model id the provider reported in the
  replies (saved call rows), the report prefers it, and a replay test checks it. The old rows
  have no `response_model`; the report shows their edited `model`.

## Failure attribution

Who or what each non-READY L1 and L2 outcome is attributed to. Condition D is not a failure case.
"Debatable" means the attribution is a judgement and not settled.

| Unit | Failure | Attributed to |
|---|---|---|
| L1 S1 | `omit_if_null_fields` lists nullable `externalRef`, `phoneNumber` (twice) | Validator rule, **debatable**. The prompt does state the rule ("optional, non-nullable") and the model did not follow it, twice |
| L1 S4 | the same `omit_if_null` listing; re-ask adds a nonexistent `PATCH /users/{userId}` | Validator rule, **debatable** (first part); model (invented endpoint) |
| L1 S3 | `SECRET_LITERAL` on a 41-character benign identifier in edge data | **Gate false positive.** Observation only: the proposal also sent `segment` on update, which would overwrite it (a model flaw), but that is not what stopped the unit |
| L2 S1 | `DUNDER_ACCESS` on `__cause__` | **Our prompt contradiction** (the runtime reference tells the model to check `__cause__`; the gate bans dunders) |
| L2 S1 | `SUPPRESSION` on `# pragma: no cover` | Model. It would have stopped the unit even with the prompt fixed |
| L2 S4 | `SYNTAX`, stray `}` inside the model's code string | Model (not the extractor; see above) |
| L2 S3 | `SYNTAX`, stray `}` inside the model's code string | Model (not the extractor) |

The `omit_if_null` rule rejects any field the target schema marks nullable. Whether a nullable
field may be dropped when null (so the target keeps its old value) is a semantics question the
frozen prompt answers one way (it defines `omit_if_null_fields` as optional, non-nullable fields).
Both the model's non-compliance with that wording and the wording itself are in play, so this is
**not settled**.

## Oracle totals reconcile

Per-unit O1 to O7 sums in `codegen-eval.md` are S1 139, S4 139, S3 123 = **401**; the 413
quoted in `milestones.md` is **401 + 12**. The 12 are the O8 (review gate) checks, 4 per
scenario, 3 scenarios. O8 lives in `test_review_gate.py`, which the standalone oracle run
executes but this evaluation's per-unit grading does not (it runs the O1 to O7 suites only, so
the O8 column is `-` and correctness is O1 to O7 anyway). Per-scenario in `milestones.md`:
143 − 4 = 139 (S1, S4) and 127 − 4 = 123 (S3).

## Known issues (deferred to v0.5, not fixed in v0.4)

1. **L2 prompt/gate contradiction:** the runtime reference tells the model to check
   `error.__cause__`, which the AST gate rejects as dunder access.
2. **`SECRET_LITERAL` false positive** on benign identifiers of 40 or more token characters
   (hit by an edge-case `externalRef` in L1 S3).
3. **`omit_if_null` rule on nullable fields:** the validator rejects it and the prompt defines
   the field as non-nullable only; whether that is the right rule is debatable and to be decided
   before any `codegen-v2` run.

None was changed here: prompts, generator, gate, validator, oracle and fixtures are as frozen.
