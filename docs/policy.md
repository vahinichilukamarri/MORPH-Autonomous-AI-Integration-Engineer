# The tool policy

Every call an agent makes to MORPH over MCP passes one gateway: **schema, floor, policy, service,
redaction, audit**, in that order ([mcp.md](mcp.md) describes the tools). The policy is plain code and a
declarative file; no model is involved in a decision. The default is deny.

## Three layers

| Layer | Where | Can the policy file change it? |
|---|---|---|
| **Floor**: what is never allowed | `backend/app/policy/floor.py` | No. A file that would weaken it is refused at load. |
| **Policy**: what is allowed above the floor | `backend/policy/morph-policy-v1.yaml` | It is the file. Versioned, hashed, locked. |
| **Session limits**: model calls and sandbox runs | the same file | Within ceilings set in code (50 model calls, 200 sandbox runs, 1,000,000 result bytes). |

Domain gates still apply after an **ALLOW**: the mapping review gate (`BLOCKED_PENDING_REVIEW`), the AST
gate, the sandbox limits. A policy decision means "may attempt"; it skips nothing.

## What a decision looks at

The evaluator reads a `CallContext` that the server builds from its own state; it has no free-text field,
so no sentence in a contract, record or model reply can change a decision (a test asserts the field set).

| Field | Where it comes from |
|---|---|
| `tool`, `effects` | the registry (`app/policy/toolspec.py`) |
| `role` | `MORPH_MCP_ROLE` of the server process |
| `environment`, `data_class` | the database: the most restrictive attributes of the systems the call involves, found by following ids (mapping run, integration, repair run) |
| `model_involved` | the tool and its arguments (condition D uses no model) |
| `facts` | computed from the raw arguments: a URL is present; a file path leaves the spec root |
| `approval` | the approvals table: valid, expired, consumed, for another call, for another policy, and so on |
| `model_calls_used`, `sandbox_runs_used` | the audit log (`CHARGE` events of the session) |

Order of precedence: **floor denial, then session limits, then policy rules (deny, then needs-approval, then
allow), then default deny.** An approval turns needs-approval into allow and nothing else.

## The floor

| Id | Rule |
|---|---|
| F01 | A tool that runs code is denied when the target system's environment is not `mock` (unknown counts as production). |
| F02 | No tool argument may be a URL; nothing fetches a remote address. |
| F03 | A file argument stays inside the spec root: no absolute path, no `..`, no NUL, no symlink or junction that leaves it. |
| F04 | A tool that is not in the registry is denied. |
| F05 | No tool may approve, override, change policy or attributes, grade, reach the benchmark, fetch, run a shell, or read files or the environment. |
| F06 | A result larger than the session cap is not returned. |
| F07 | Known secret values and secret-shaped strings are removed from every result, audit payload and log line. |
| F08 | Generated test data (and failure details, and model output) is returned only for synthetic data. |
| F09 | An approval never overrides a denial. |
| F10 | A policy may not set a limit above the ceilings in code. |

## Policy v1 (default deny)

| Rule | Decision | What it says |
|---|---|---|
| P01 | allow | read-only tools, for both roles |
| P02 | allow | an operator may ingest a contract file |
| P03 | allow | an operator may generate with the deterministic generator (condition D) |
| P04 | allow | an operator may run the generated tests in the sandbox |
| P05 | allow | an operator may use the model on a system whose data is `synthetic` |
| P06 | needs approval | a model call on `internal`, `restricted` or `unclassified` data |
| P07 | deny | a reader may not write, run code or call the model |
| L01 | deny | the session's model-call or sandbox-run budget is used up (limits: 40 and 60) |

A system with no recorded attributes is `unknown` / `unclassified`, so code does not run for it (F01) and a
model call on it needs approval (P06) until a human records it with the approver token.

## Versions, the lock and what they do not protect

* `backend/policy/policy.lock` names the active version and its hash. The server hashes the file at
  start-up and **refuses to start on a mismatch**. There is no reload and no tool or route that changes
  a policy.
* `backend/policy/versions.json` records the hash of every released version. A released file never changes
  (`python -m app.policy.check`, run in CI, fails otherwise). A new version is a new file, a new entry, a
  changed lock and passing tests. A version that loosens relative to the previous one (more limits, a new
  allow rule, a removed deny rule) must say so in `loosens:`, or the check fails.
* The hash is of the parsed policy, so comments and key order do not change it.
* **The lock lives in the repository.** It guards against drift and against edits outside review. It does
  **not** guard against someone with write access to the repository, who can change a file and the lock
  together.

## Attributes, approvals and the audit log

* **System attributes** (`environment`, `data_class`) are written only by `PUT
  /systems/{id}/policy-attributes` with the approver token (`MORPH_APPROVER_TOKEN`, environment only), and
  every change is an audit event. No MCP tool can write them; a test scans the code for any other writer.
* **Approvals** are decided only at `POST /approvals/{id}/decide` with the approver token. They are bound
  to the tool, the hash of the arguments and the policy hash; single use; expiring (30 minutes).
* **The audit log** (`audit_events`) is append-only by a database trigger (UPDATE, DELETE and TRUNCATE are
  refused) and hash-chained: each event holds the previous event's hash and its own over all its fields.
  `GET /audit/verify` recomputes the chain. Appends take an advisory lock in their own short transaction, so
  two processes writing at once keep one linear chain. **Known limit:** removing the *last* events leaves a
  shorter, valid chain; detecting that needs an anchor outside the database, which v0.6 does not have. A
  database owner can switch the trigger off; the hash chain still catches an edit or a removal in the
  middle.

## Threats and controls

| Threat | Control | Test |
|---|---|---|
| Injected instructions in contracts, fields, records, generated code or model replies | decisions use only server-derived fields; arguments are closed schemas; no approve/policy/attribute/grading tool; untrusted text is delimited; agent text never reaches a trusted prompt section (no `requirement` argument) | the injection corpus ([policy-eval.md](policy-eval.md)) |
| Secret exfiltration | redaction of known values and patterns on results, audit and logs | the redaction corpus |
| Record-data exfiltration | metadata-only tools; test data, failure details and model output only for synthetic data; result size cap | corpus and tool tests |
| SSRF and path escape | F02, F03 | must-deny corpus |
| Code running against something that is not a mock | F01 (**verified on a synthetic target**; there is no real production system here) | must-deny corpus |
| Over-broad scope and runaway cost | roles, session limits counted inside repair runs | budget tests |
| Approval abuse | binding, single use, expiry, policy hash | approval tests |
| Policy loosening or tampering | floor check at load, lock, versions, no reload | loader tests |
| Audit tampering | trigger plus hash chain | tamper corpus |
| Going around the gateway | one `tools/call` handler; a registry test over every tool and an import-direction test | registry tests |

None of this measures how an external agent or model resists injection: see the limits in
[policy-eval.md](policy-eval.md).
