# Code generation evaluation (v0.4)

Run date: 2026-10-07. Units: **N = 18** (scenario x input set x condition); models: groq/openai/gpt-oss-120b.

N is small. Differences of one unit are within noise; nothing here is statistically
significant. Generator v1 and codegen prompts v1 were frozen (golden hashes) before the
first real run; any later change is a separate, labelled run.

Conditions: **D** deterministic compile and heuristic strategy, no model. **L1** D with a
model-proposed, contract-validated sync strategy and edge-case inputs. **L2** D with a
free-form model-written sync module behind the static gate. Input sets: **approved** =
reference pipelines stored as human-approved mappings (isolates codegen from mapping
quality); **as_proposed** = the real v0.3 proposals with the review gate and no human edit.

## Outcome of each unit

| Unit | Cond. | Status | Gate (ast/ruff/mypy) | Gen. tests | Oracle correct | Notes |
|---|---|---|---|---|---|---|
| S1 approved | D | `READY` | pass/pass/pass | 13/13 | yes |  |
| S1 approved | L1 | `LLM_INVALID` | - | - | - | model output invalid: omit_if_null_fields: 'externalRef' is not optional and non-nullable; omit_if_null_fields: 'phoneNu |
| S1 approved | L2 | `GATE_FAILED` | FAIL/not run/not run | - | - | ast: SUPPRESSION lint and type suppression comments |
| S4 approved | D | `READY` | pass/pass/pass | 13/13 | yes |  |
| S4 approved | L1 | `LLM_INVALID` | - | - | - | model output invalid: target: there is no PATCH by id at '/users/{userId}'; omit_if_null_fields: 'externalRef' is not op |
| S4 approved | L2 | `GATE_FAILED` | FAIL/not run/not run | - | - | ast: SYNTAX unmatched '}' (integration/sync.py, line 177) |
| S3 approved | D | `READY` | pass/pass/pass | 16/16 | yes |  |
| S3 approved | L1 | `GATE_FAILED` | FAIL/not run/not run | - | - | ast: SECRET_LITERAL string looks like a secret or token |
| S3 approved | L2 | `GATE_FAILED` | FAIL/not run/not run | - | - | ast: SYNTAX unmatched '}' (integration/sync.py, line 145) |
| S1 as_proposed | D | `BLOCKED_PENDING_REVIEW` | - | - | - | accountState: NEEDS_REVIEW; tier: NEEDS_REVIEW |
| S1 as_proposed | L1 | `BLOCKED_PENDING_REVIEW` | - | - | - | accountState: NEEDS_REVIEW; tier: NEEDS_REVIEW |
| S1 as_proposed | L2 | `BLOCKED_PENDING_REVIEW` | - | - | - | accountState: NEEDS_REVIEW; tier: NEEDS_REVIEW |
| S4 as_proposed | D | `BLOCKED_PENDING_REVIEW` | - | - | - | accountState: NEEDS_REVIEW; serviceTier: NEEDS_REVIEW |
| S4 as_proposed | L1 | `BLOCKED_PENDING_REVIEW` | - | - | - | accountState: NEEDS_REVIEW; serviceTier: NEEDS_REVIEW |
| S4 as_proposed | L2 | `BLOCKED_PENDING_REVIEW` | - | - | - | accountState: NEEDS_REVIEW; serviceTier: NEEDS_REVIEW |
| S3 as_proposed | D | `BLOCKED_PENDING_REVIEW` | - | - | - | segment: UNRESOLVED |
| S3 as_proposed | L1 | `BLOCKED_PENDING_REVIEW` | - | - | - | segment: UNRESOLVED |
| S3 as_proposed | L2 | `BLOCKED_PENDING_REVIEW` | - | - | - | segment: UNRESOLVED |

## Oracle results by category (passed/total checks)

| Unit | Cond. | O1 | O2 | O3 | O4 | O5 | O6 | O7 | O8 |
|---|---|---|---|---|---|---|---|---|---|
| S1 approved | D | 18/18 | 19/19 | 11/11 | 20/20 | 29/29 | 37/37 | 5/5 | - |
| S4 approved | D | 18/18 | 19/19 | 11/11 | 20/20 | 29/29 | 37/37 | 5/5 | - |
| S3 approved | D | 17/17 | 19/19 | 11/11 | 9/9 | 29/29 | 33/33 | 5/5 | - |

## By condition

| Condition | Units | Reached READY | Gate passed | Oracle-graded | integration_correct | Blocked / LLM invalid |
|---|---|---|---|---|---|---|
| D | 6 | 3/6 | 3/6 | 3 | 3/3 | 3 |
| L1 | 6 | 0/6 | 0/6 | 0 | - | 5 |
| L2 | 6 | 0/6 | 0/6 | 0 | - | 3 |

## Model use (real counts)

| Unit | Cond. | Calls | Input tok. | Output tok. | Reasoning tok. | Latency ms | Invalid |
|---|---|---|---|---|---|---|---|
| S1 approved | L1 | 2 | 8781 | 2092 | 674 | 5717 | 2 |
| S1 approved | L2 | 1 | 4002 | 1859 | 382 | 9375 | 0 |
| S4 approved | L1 | 2 | 8792 | 2071 | 665 | 5983 | 2 |
| S4 approved | L2 | 1 | 4008 | 1773 | 265 | 4672 | 0 |
| S3 approved | L1 | 2 | 8690 | 1964 | 600 | 5733 | 1 |
| S3 approved | L2 | 1 | 3951 | 1526 | 168 | 5375 | 0 |

## How to read this

* `Gen. tests` are the deterministic generated tests (compiled code against the DSL on
  sample records). They are reported apart from the oracle and never mixed into it.
* `Oracle correct` means no failed check in O1 to O7; O8 (the review gate) is shown but is
  not part of correctness.
* A `BLOCKED_*` or `LLM_INVALID` status is a result: no code was produced and none was run.
* `as_proposed` mappings are rebuilt, with no model call, from the responses saved by the
  real v0.3 evaluation (the committed replay for S1, the local response store for the
  others), validated with validator v1 as in that run. A unit whose required fields were
  left in review is blocked exactly as a person would see it.
* `not run` in the gate column means an earlier stage failed, so the later stage was never
  executed (a failing gate never lets code run).
* Every oracle check passed or failed on its own; results are never weighted or combined.
* The reading of these numbers, with what the model proposed and wrote, is in
  `codegen-eval-findings.md`.
