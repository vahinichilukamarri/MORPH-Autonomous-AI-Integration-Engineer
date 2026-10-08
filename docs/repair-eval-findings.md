# Repair evaluation: findings (v0.5, fixed-start run)

Companion to the generated report [repair-eval.md](repair-eval.md), whose numbers all derive from the
committed replays and results. This page explains them. **N = 1 per unit and 3 units per condition:
nothing here is statistically significant, and a difference of one unit is noise.** Written after the
run, against the interpretation rules fixed beforehand in
[plans/v0.5-fixed-start-preregistration.md](plans/v0.5-fixed-start-preregistration.md).

## What was run

* Phase `fixed`: L1R and L2R on the approved S1, S3 and S4. Attempt 0 is the recorded v0.4 reply
  (no call); up to 3 repairs follow. Groq `openai/gpt-oss-120b`, low reasoning effort, temperature 0, at
  most 4,000 output tokens: the v0.4 settings. N = 1, no tuning, no unit run twice.
* Harness commit `d80d9f0`, working tree clean at the start. Prompts, graph, guards and caps were frozen
  by golden hashes before the first call (amendment A15 re-froze the L1 repair hashes before it).
* One start, no pause, no rate-limit wait, no `SIZE_REJECTED`, no unit stopped before a call. 15 real
  calls (cap 20). Seeded attempt-0 replies were served from the v0.4 replays and cost no call.
* Reference: condition **D** (human-approved mappings, no model) reached READY and passed the oracle on
  all three scenarios (`codegen-eval.md`). v0.4 L1 and L2 on the same approved inputs reached READY on
  none of them.

## Result

| Unit | Exposure tag | Outcome | Repairs used | Oracle (O1 to O7) |
|---|---|---|---|---|
| S1 L1R | `PROMPT_GAP` | READY | 3 | all pass (as D) |
| S4 L1R | `PROMPT_GAP` | READY | 1 | all pass (as D) |
| S3 L1R | `SECRET_LITERAL_FALSE_POSITIVE` | READY, **gate-passing, incorrect** | 2 | 14 checks fail |
| S1 L2R | `CAUSE_CONTRADICTION` | `HUMAN_REVIEW_REQUIRED` (exhausted at GUARD) | - | not graded |
| S4 L2R | `MODEL` | `HUMAN_REVIEW_REQUIRED` (exhausted at RUFF) | - | not graded |
| S3 L2R | `MODEL` | `HUMAN_REVIEW_REQUIRED` (exhausted at RUFF) | - | not graded |

* **L1R: 3 of 3 reached READY and 2 of 3 are correct.** **L2R: 0 of 3 reached READY.** Both conditions
  are measurable (no unit was not run).
* **Exposed and unexposed, separately.** The four exposed units (S1 L1R, S4 L1R, S3 L1R, S1 L2R) gave
  three READY, of which two are correct. The two unexposed units (S3 L2R, S4 L2R, model failures) gave
  none. Neither split replaces the other, and with one unit each neither supports a rate.
* The oracle graded each READY unit once, after its loop, and its verdict never reached the repair code.

## The pre-registered prediction held

L1R S3 reached READY and failed the oracle, as predicted. Its final strategy still sends `segment` in
`target_update_fields` exactly as attempt 0 did: the repair changed its `omit_if_null_fields` list and
its edge records, and left `segment` alone, because the gate, the generated tests and the smoke test carry no signal about
`segment`. This is "gate-passing, incorrect", not a repair success. The 14 failed checks span O1, O2,
O4, O5, O6 and O7; the stored oracle summary names the checks but not why each failed, so I attribute the
failure to the segment-overwrite flaw because the strategy still has it and the prediction named it, not
because each check was traced.

## What the feedback did

* **L1R, PROPOSAL feedback.** The validator rules the v0.4 first replies broke (`UPSERT` update fields
  must equal create fields, and `omit_if_null_fields`) were cleared on the next attempt in all three
  units. For S1 and S4 (the `PROMPT_GAP` units) the model fixed a rule the frozen prompt never states,
  once told. S4 reached READY on that single repair.
* **L1R, GUARD feedback.** S1's first repair fixed the validator errors by dropping `userId` from the
  written fields, and guard G5 rejected it ("mapped fields written nowhere: userId"). The next attempt
  passed G5. That is a regression the validator alone would not have caught.
* **L1R, AST feedback and the edge records (A15).** S1 and S3 each had a `SECRET_LITERAL` finding on
  long strings in model-proposed edge records. The feedback named the record and field (for S1: edge
  record 3, fields `first_name`, `last_name` and a computed value) and gave only lengths. Both units
  cleared it on the next attempt. The prompt contained the numbered records at that point. One unit each
  cannot show the echo fix caused this, only that the failure it was meant to prevent did not occur.
* **L2R, the conflicting `__cause__` hint.** The gate rejected `__cause__` as dunder access on a repair
  attempt in **all three** L2R units (S1 at attempt 1, S3 and S4 at attempt 2), not only the tagged one,
  because the original task tells the model to use it. In all three the next attempt removed it. This is
  compliance with the repair prompt's "follow the check" instruction, as pre-registered; it says nothing
  about whether the model could discover the conflict.
* **L2R, other failures.** After the `SYNTAX` feedback on the stray-brace replies (S3, S4) the next
  attempt added lint-suppression comments, which guard G3 rejected, and the following one cleared them.
  S1's seeded failure was already G3. Ruff and mypy findings (`F401`, `UP035`, `UP006`, type errors)
  appeared in the last attempt that reached the gate (attempt 3 for S3 and S4, attempt 2 for S1) and were
  not repaired within the budget.
* **L2R, the budget.** The gate reports one stage at a time, so a unit that has to clear PROPOSAL or
  SYNTAX, then G3, then DUNDER_ACCESS, then ruff and mypy needs four or five rounds, and only three are
  allowed. S3 and S4 had reached the fourth stage (RUFF) when the budget ended; whether one more attempt
  would have finished them is not known and is not claimed.
* **L2R S1, attempt 3.** Guard G4 rejected it ("type escapes rose from 13 to 17"): the model reacted to
  the ruff and mypy feedback by adding type escapes. G4 runs before the gate, so whether that attempt's
  ruff and mypy findings were fixed was never observed.
* **Shadow guards (G6, G7)** did not trip on any attempt.
* The report's "which feedback resolved what" table lists, per attempt, the codes cleared, kept and new.
  Cleared means absent from the next attempt's feedback; because the gate stops at the first failing
  stage, a code that is absent may simply not have been checked yet.

## Cost (real counts; the report has the per-unit table)

| | Calls | Input tok. | Output tok. | Reasoning tok. | Provider total tok. | Model latency |
|---|---|---|---|---|---|---|
| L1R (3 units) | 6 | 31,380 | 5,890 | 1,733 | 37,270 | 19,718 ms |
| L2R (3 units) | 9 | 49,824 | 12,809 | 1,237 | 62,633 | 40,748 ms |
| Total | 15 | 81,204 | 18,699 | 2,970 | 99,903 | 60,466 ms |

* `total_tokens` equals input + output on 15 of 15 calls and input + output + reasoning on none, so the
  provider's output count **includes** the reasoning tokens. This settles the question v0.4 left open.
* The harness's token cap counter reads 102,873, which is the provider total plus the reasoning tokens
  counted a second time (2,970), as documented: it is an upper bound, and it was below the 160,000 cap.
* Per call, the provider total was 6,049 to 7,113 tokens and latency 2.7 to 6.5 seconds.
* Seeded attempt 0 (v0.4, not part of the repair cost) is in the report separately; its `total_tokens`
  was not recorded in v0.4.

## Size and rate limits

* The estimator was above the provider's total on 14 of 15 calls (from -1.4% to +12.7%). The largest
  estimate was 7,928 tokens, the largest provider total 7,113, both under 8,000. Nothing came near the
  8,800 hard stop, and the provider rejected nothing as too large.
* The response headers showed 8,000 tokens per minute and 1,000 requests per day, as in v0.4. The daily
  token limit is not exposed in the headers; the run used 99,903 provider tokens without hitting a limit,
  which shows only that the key's daily limit is at least that.

## What this does not show

* One run of one model, one unit per cell. A different seed or a fresh start could land differently,
  and a one-unit difference between L1R and L2R is within noise.
* L1R and L2R are not compared as "which is better": the budget structure, the prompt gaps and the
  conflicting hint differ by design, and the exposure tags mark where the v0.4 failures were not the
  model's alone.
* The fixed start isolates the repair effect from regeneration variance; it says nothing about a model
  that starts from scratch. The fresh-start phase has not been run and needs its own approval.
* READY is a statement about the gate, the generated tests and the smoke test. Only the oracle speaks
  to correctness, and it was applied to the READY units only.
* The oracle revision r1 (post-hoc, from v0.4) applies here too; D's numbers are from that oracle.

## Reproducing this page's numbers

`bench/replays/repair/fixed/` holds the 15 real replies and the per-attempt outcomes;
`bench/results/repair-v0.5/fixed/` holds the results and the provenance (harness commit, versions, model
id from the replies, observed limits, file hashes, `post_run_edits: []`). `uv run python -m
scripts.render_reports --check`, run from `bench/`, regenerates `repair-eval.md` and fails on any
difference or undeclared edit. The sandbox CI job replays the committed run offline through the real gate
and smoke test.
