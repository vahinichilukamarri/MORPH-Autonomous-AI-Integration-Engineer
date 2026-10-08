# Repair evaluation (v0.5, fixed-start)

Run date: 2026-10-08. Model reported by the replies: `openai/gpt-oss-120b`. Units: **N = 6** (scenario x condition, approved inputs), one run each.

N is 1 per unit and 3 units per condition: nothing here is statistically significant and a
difference of one unit is noise. The experiment was frozen and pre-registered before the run
(`docs/plans/v0.5-fixed-start-preregistration.md`); failures are results and nothing was tuned.
Model-dependent values below are derived from the committed replays; only the oracle results,
start mode and exposure tags come from the committed results file. `render_reports --check`
fails if the two disagree.

Conditions: **L1R** the model proposes the sync strategy and edge records, repaired; **L2R**
the model writes the sync module, repaired. Fixed start: attempt 0 is the recorded v0.4 reply
(no call), so the repair effect is measured from the same starting failure. Reference: condition
**D** (no model) in `codegen-eval.md`.

## Outcome of each unit

| Unit | Exposure tag | Outcome | Repairs used | Model turns | Real calls |
|---|---|---|---|---|---|
| S1 L1R | `PROMPT_GAP` | READY, oracle correct | 3 | 4 | 3 |
| S4 L1R | `PROMPT_GAP` | READY, oracle correct | 1 | 2 | 1 |
| S3 L1R | `SECRET_LITERAL_FALSE_POSITIVE` | READY, **gate-passing, incorrect** | 2 | 3 | 2 |
| S1 L2R | `CAUSE_CONTRADICTION` | `HUMAN_REVIEW_REQUIRED` (EXHAUSTED:GUARD) | - | 4 | 3 |
| S4 L2R | `MODEL` | `HUMAN_REVIEW_REQUIRED` (EXHAUSTED:RUFF) | - | 4 | 3 |
| S3 L2R | `MODEL` | `HUMAN_REVIEW_REQUIRED` (EXHAUSTED:RUFF) | - | 4 | 3 |

## By condition

| Condition | Units | Reached READY | Oracle correct (of graded) | READY but incorrect | Human review required | Not run | Measurable |
|---|---|---|---|---|---|---|---|
| L1R | 3 | 3/3 | 2/3 | 1 | 0/3 | 0/3 | yes |
| L2R | 3 | 0/3 | - | - | 3/3 | 0/3 | yes |

READY means the proposal passed the static gate, the generated tests and the smoke test. It is
not correctness; the oracle decides that. A READY unit that fails the oracle is reported as
READY-but-incorrect and is not a success. Units not run stay in every denominator.

## Oracle results on READY units (passed/total checks)

| Unit | O1 | O2 | O3 | O4 | O5 | O6 | O7 | O8 |
|---|---|---|---|---|---|---|---|---|
| S1 L1R | 18/18 | 19/19 | 11/11 | 20/20 | 29/29 | 37/37 | 5/5 | - |
| S4 L1R | 18/18 | 19/19 | 11/11 | 20/20 | 29/29 | 37/37 | 5/5 | - |
| S3 L1R | 13/17 | 18/19 | 11/11 | 8/9 | 24/29 | 31/33 | 4/5 | - |

Failed checks (O1 to O7 decide correctness; O8 is shown, not counted):

| Unit | Check | Counts against correctness |
|---|---|---|
| S3 L1R | `O1.imported-user-updates-its-customer:values` | yes |
| S3 L1R | `O1.first-space-split-and-disabled:values` | yes |
| S3 L1R | `O1.blocked-becomes-suspended:values` | yes |
| S3 L1R | `O1.no_missing_extra_or_wrong_target_records` | yes |
| S3 L1R | `O4.n=101:target_holds_exactly_the_expected_records` | yes |
| S3 L1R | `O5.http_500_on_target:converges_to_the_expected_state_without_duplicates` | yes |
| S3 L1R | `O5.http_500_on_source:converges_to_the_expected_state_without_duplicates` | yes |
| S3 L1R | `O5.http_429_on_target:converges_to_the_expected_state_without_duplicates` | yes |
| S3 L1R | `O5.malformed_json_on_target:converges_to_the_expected_state_without_duplicates` | yes |
| S3 L1R | `O5.latency:state_is_correct` | yes |
| S3 L1R | `O7.unrelated_source_drift:no_false_alarm` | yes |
| S3 L1R | `O6.unknown_enum:target_state_is_exactly_the_expected_one` | yes |
| S3 L1R | `O6.bad_created_at:target_state_is_exactly_the_expected_one` | yes |
| S3 L1R | `O2.target_state_is_exactly_the_expected_one` | yes |

## Attempts

| Unit | Attempt | Source | Failed stage | Feedback codes | Guards tripped (enforced) | Shadow guards | Finish reason |
|---|---|---|---|---|---|---|---|
| S1 L1R | 0 | seeded (v0.4 reply) | PROPOSAL | PROPOSAL.UPSERT, PROPOSAL.omit_if_null_fields, PROPOSAL.omit_if_null_fields | - | - | not recorded |
| S1 L1R | 1 | model | GUARD | GUARD.G5, GUARD.G5, GUARD.G5 | G5, G5, G5 | - | stop |
| S1 L1R | 2 | model | AST | AST.SECRET_LITERAL, AST.SECRET_LITERAL, AST.SECRET_LITERAL | - | - | stop |
| S1 L1R | 3 | model | READY | - | - | - | stop |
| S4 L1R | 0 | seeded (v0.4 reply) | PROPOSAL | PROPOSAL.UPSERT, PROPOSAL.omit_if_null_fields, PROPOSAL.omit_if_null_fields | - | - | not recorded |
| S4 L1R | 1 | model | READY | - | - | - | stop |
| S3 L1R | 0 | seeded (v0.4 reply) | PROPOSAL | PROPOSAL.omit_if_null_fields, PROPOSAL.omit_if_null_fields | - | - | not recorded |
| S3 L1R | 1 | model | AST | AST.SECRET_LITERAL, AST.SECRET_LITERAL | - | - | stop |
| S3 L1R | 2 | model | READY | - | - | - | stop |
| S1 L2R | 0 | seeded (v0.4 reply) | GUARD | GUARD.G3 | G3 | - | not recorded |
| S1 L2R | 1 | model | AST | AST.DUNDER_ACCESS | - | - | stop |
| S1 L2R | 2 | model | RUFF | RUFF.F401, RUFF.UP035, RUFF.UP035, RUFF.UP006, RUFF.UP006, MYPY.TYPE_ERROR, MYPY.TYPE_ERROR, MYPY.TYPE_ERROR | - | - | stop |
| S1 L2R | 3 | model | GUARD | GUARD.G4 | G4 | - | stop |
| S4 L2R | 0 | seeded (v0.4 reply) | AST | AST.SYNTAX | - | - | not recorded |
| S4 L2R | 1 | model | GUARD | GUARD.G3 | G3 | - | stop |
| S4 L2R | 2 | model | AST | AST.DUNDER_ACCESS | - | - | stop |
| S4 L2R | 3 | model | RUFF | RUFF.F401, RUFF.F401, RUFF.UP035, RUFF.UP035, RUFF.F401, RUFF.UP006, RUFF.UP006, MYPY.TYPE_ERROR | - | - | stop |
| S3 L2R | 0 | seeded (v0.4 reply) | AST | AST.SYNTAX | - | - | not recorded |
| S3 L2R | 1 | model | GUARD | GUARD.G3 | G3 | - | stop |
| S3 L2R | 2 | model | AST | AST.DUNDER_ACCESS | - | - | stop |
| S3 L2R | 3 | model | RUFF | RUFF.F401, RUFF.UP035, RUFF.UP035, RUFF.F401, RUFF.F401, RUFF.F401, RUFF.F401, RUFF.UP006 | - | - | stop |

## Which feedback resolved what

| Unit | Attempts | Result | Codes cleared | Codes still present | New codes |
|---|---|---|---|---|---|
| S1 L1R | 0 to 1 | passed PROPOSAL, failed later at GUARD | PROPOSAL.UPSERT, PROPOSAL.omit_if_null_fields | - | GUARD.G5 |
| S1 L1R | 1 to 2 | passed GUARD, failed later at AST | GUARD.G5 | - | AST.SECRET_LITERAL |
| S1 L1R | 2 to 3 | READY; the AST feedback resolved it | AST.SECRET_LITERAL | - | - |
| S4 L1R | 0 to 1 | READY; the PROPOSAL feedback resolved it | PROPOSAL.UPSERT, PROPOSAL.omit_if_null_fields | - | - |
| S3 L1R | 0 to 1 | passed PROPOSAL, failed later at AST | PROPOSAL.omit_if_null_fields | - | AST.SECRET_LITERAL |
| S3 L1R | 1 to 2 | READY; the AST feedback resolved it | AST.SECRET_LITERAL | - | - |
| S1 L2R | 0 to 1 | passed GUARD, failed later at AST | GUARD.G3 | - | AST.DUNDER_ACCESS |
| S1 L2R | 1 to 2 | passed AST, failed later at RUFF | AST.DUNDER_ACCESS | - | MYPY.TYPE_ERROR, RUFF.F401, RUFF.UP006, RUFF.UP035 |
| S1 L2R | 2 to 3 | went back from RUFF to GUARD | MYPY.TYPE_ERROR, RUFF.F401, RUFF.UP006, RUFF.UP035 | - | GUARD.G4 |
| S4 L2R | 0 to 1 | went back from AST to GUARD | AST.SYNTAX | - | GUARD.G3 |
| S4 L2R | 1 to 2 | passed GUARD, failed later at AST | GUARD.G3 | - | AST.DUNDER_ACCESS |
| S4 L2R | 2 to 3 | passed AST, failed later at RUFF | AST.DUNDER_ACCESS | - | MYPY.TYPE_ERROR, RUFF.F401, RUFF.UP006, RUFF.UP035 |
| S3 L2R | 0 to 1 | went back from AST to GUARD | AST.SYNTAX | - | GUARD.G3 |
| S3 L2R | 1 to 2 | passed GUARD, failed later at AST | GUARD.G3 | - | AST.DUNDER_ACCESS |
| S3 L2R | 2 to 3 | passed AST, failed later at RUFF | AST.DUNDER_ACCESS | - | RUFF.F401, RUFF.UP006, RUFF.UP035 |

## Tokens and latency of the repair calls (real counts)

| Unit | Calls | Input tok. | Output tok. | Reasoning tok. | Total tok. (provider) | Latency ms |
|---|---|---|---|---|---|---|
| S1 L1R | 3 | 15709 | 2792 | 733 | 18501 | 8734 |
| S4 L1R | 1 | 5225 | 1239 | 485 | 6464 | 4859 |
| S3 L1R | 2 | 10446 | 1859 | 515 | 12305 | 6125 |
| S1 L2R | 3 | 16870 | 4423 | 410 | 21293 | 14999 |
| S4 L2R | 3 | 16646 | 4321 | 456 | 20967 | 13734 |
| S3 L2R | 3 | 16308 | 4065 | 371 | 20373 | 12015 |

Provider totals: `total_tokens` equals input + output on 15 of 15 calls and input + output + reasoning on 0 of 15.

## Seeded attempt 0 (the recorded v0.4 replies, not part of the repair cost)

| Unit | Attempt | Input tok. | Output tok. | Reasoning tok. | Total tok. |
|---|---|---|---|---|---|
| S1 L1R | 0 | 4080 | 1112 | 455 | not recorded |
| S4 L1R | 0 | 4086 | 1147 | 424 | not recorded |
| S3 L1R | 0 | 4029 | 1175 | 385 | not recorded |
| S1 L2R | 0 | 4002 | 1859 | 382 | not recorded |
| S4 L2R | 0 | 4008 | 1773 | 265 | not recorded |
| S3 L2R | 0 | 3951 | 1526 | 168 | not recorded |

## How to read this

* The feedback stage order is PROPOSAL, GUARD, AST, RUFF, MYPY, TESTS, SMOKE. The gate stops at
  the first failing stage, so later stages are not seen until earlier ones pass.
* L1R S3 was pre-registered as likely READY-but-incorrect (its segment-overwrite flaw has no
  runtime signal). L2R S1 measures compliance with the repair prompt's instruction to follow a
  check over a conflicting hint, not discovery of the conflict.
* `SIZE_REJECTED` and size-stopped units are infrastructure results, not model results.
* The reading of these numbers is in `repair-eval-findings.md`.
