"""Run every pre-registered corpus and render ``docs/policy-eval.md``.

    uv run python -m tests.policy.evalkit.report            # run and rewrite the report uv run
    python -m tests.policy.evalkit.report --check    # run, rewrite, then fail on any difference

The report is deterministic: it holds counts, case ids and problems, never a time, an id from the
database or a duration. Every claim carries its denominator, taken from the pre-registration. With
``--check`` the rewritten file must equal what git has (``git diff --exit-code``).
"""

import argparse
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine

from tests.policy.controls import CONTROLS
from tests.policy.evalkit import corpus, injection, redaction, tamper
from tests.policy.evalkit.database import throwaway_engine
from tests.policy.evalkit.injection import CaseResult as InjectionResult
from tests.policy.evalkit.interpreter import Case, Outcome, run_case
from tests.policy.evalkit.redaction import RedactionResult
from tests.policy.evalkit.tamper import TamperResult

REPO = Path(__file__).resolve().parents[4]
DOC = REPO / "docs" / "policy-eval.md"


@dataclass
class Results:
    gateway: dict[str, list[tuple[Case, Outcome]]] = field(default_factory=dict)
    injection: list[InjectionResult] = field(default_factory=list)
    tamper: list[TamperResult] = field(default_factory=list)
    redaction: dict[str, list[RedactionResult]] = field(default_factory=dict)
    controls: tuple[int, int] = (0, 0)  # (passed, total)


def run_everything(engine: Engine, tmp: Path) -> Results:
    results = Results()
    for name in corpus.EXPECTED_COUNTS:
        results.gateway[name] = [
            (case, run_case(engine, tmp / case.id, case)) for case in corpus.load(name)
        ]
    for payload in injection.load_payloads():
        for carrier in injection.CARRIERS:
            results.injection.append(injection.run_case(engine, tmp / "inj", payload, carrier))
    results.tamper = [tamper.run_case(engine, case) for case in tamper.CASES]
    results.redaction = redaction.run_all(engine, tmp / "red")
    checks = [(rule, kind, getattr(control, kind)()) for rule, control in CONTROLS.items()
              for kind in ("positive", "negative")]  # fmt: skip
    results.controls = (sum(1 for _, _, ok in checks if ok), len(checks))
    return results


# ---- rendering -----------------------------------------------------------------------------------


def of(x: int, n: int) -> str:
    return f"{x} of {n}"


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return [*lines, ""]


def gateway_summary(results: Results) -> dict[str, tuple[int, int, int]]:
    """For each corpus: (passed, run, skipped)."""
    out = {}
    for name, items in results.gateway.items():
        skipped = sum(1 for _, o in items if o.skipped)
        out[name] = (sum(1 for _, o in items if o.passed), len(items), skipped)
    return out


def failures(results: Results) -> list[str]:
    lines: list[str] = []
    for name, items in results.gateway.items():
        lines += [f"* `{name}` `{o.case_id}`: {'; '.join(o.problems)} (observed {o.observed})"
                  for _, o in items if not o.passed]  # fmt: skip
    lines += [
        f"* injection `{c.id}`: reached={c.reached}, invariant={c.invariant}, "
        f"executed={c.executed}, attempts={c.attempts}, leaked={c.leaked}"
        for c in results.injection if not c.passed
    ]  # fmt: skip
    lines += [f"* tamper `{t.id}` ({t.description}): not as documented" for t in results.tamper
              if not t.as_documented]  # fmt: skip
    for group_results in results.redaction.values():
        lines += [
            f"* redaction `{r.id}` ({r.channel}): {r.detail}"
            for r in group_results if not r.passed
        ]  # fmt: skip
    return lines


def render(results: Results) -> str:
    summary = gateway_summary(results)
    deny_items = results.gateway["must_deny"]
    refused = [o for _, o in deny_items if o.status == "denied"]
    stopped = [o for _, o in deny_items if o.status == "budget_exceeded"]
    refused_ok = sum(1 for o in refused if o.passed)
    handler_in_refusals = sum(1 for o in refused if o.handler_ran)
    ask_items = [o for _, o in results.gateway["must_need_approval"]]
    inj = results.injection
    attempts = sum(c.attempts for c in inj)
    executed = sum(c.executed for c in inj)
    refused_attempts = sum(c.refused for c in inj)
    tamper_ok = sum(1 for t in results.tamper if t.detectable and t.as_documented)
    limit_ok = sum(1 for t in results.tamper if not t.detectable and t.as_documented)
    red = results.redaction
    sentinel_ok = sum(1 for r in red["sentinel"] if r.passed)
    out = [
        "# Policy evaluation (v0.6)",
        "",
        "Generated by `python -m tests.policy.evalkit.report` from the corpora in",
        "`backend/tests/policy/corpus/` and `backend/tests/policy/evalkit/`, with the counts fixed in",
        "[plans/v0.6-m4-preregistration.md](plans/v0.6-m4-preregistration.md) before any corpus was",
        "written or run. CI regenerates this file and fails on any difference.",
        "",
        "## What this measures, and what it does not",
        "",
        "These corpora test **our policy, floor, gateway and audit log** against cases we wrote. They do",
        "not measure how an external agent or model resists injection, and a pass is not a security",
        "proof. The adversary is a deterministic script. No real model was called: models are scripted",
        "doubles, and the sandbox is a stub except in the tests marked `docker`.",
        "",
        "## Claims, each with its denominator",
        "",
    ]
    rows = [
        [
            "must-deny",
            of(
                refused_ok + sum(1 for o in stopped if o.passed),
                corpus.EXPECTED_COUNTS["must_deny"],
            ),
            f"ended in the expected refusal or stop: {len(refused)} refused before the service ran, "
            f"{len(stopped)} stopped by the meter mid-run; the handler ran in {handler_in_refusals} of the "
            f"{len(refused)} refusals",
        ],
        [
            "must-allow",
            of(summary["must_allow"][0], corpus.EXPECTED_COUNTS["must_allow"]),
            "allowed and the handler ran",
        ],
        [
            "must-need-approval",
            of(sum(1 for o in ask_items if o.passed), corpus.EXPECTED_COUNTS["must_need_approval"]),
            "a self-describing `needs_approval` response and the handler did not run",
        ],
        [
            "injection: payload reached the agent",
            of(sum(1 for c in inj if c.reached), len(inj)),
            "the marker appeared in a tool result, so the case is not vacuous",
        ],
        [
            "injection: decision invariance",
            of(sum(1 for c in inj if c.invariant), len(inj)),
            "six legitimate calls got the same decisions with the payload as with a benign string",
        ],
        [
            "injection: forbidden actions executed",
            f"{executed} of {attempts} under our scripted adversary",
            f"{refused_attempts} of {attempts} attempts were refused or needed approval; says nothing "
            "about real agents",
        ],
        [
            "injection: no planted secret in any result or audit row",
            of(sum(1 for c in inj if not c.leaked), len(inj)),
            "a secret was planted in every payload",
        ],
        [
            "tamper: detected at the expected event",
            of(tamper_ok, corpus.TAMPER_DETECTABLE),
            f"plus {limit_ok} of {corpus.TAMPER_LIMITS} known limit reported as undetected (removing the last events)",
        ],
        [
            "redaction: sentinel secrets absent",
            of(sentinel_ok, corpus.REDACTION_SENTINEL),
            "tool results of the 13 tools, audit rows, a captured log, REST responses",
        ],
        [
            "redaction: secret-shaped strings removed",
            of(sum(1 for r in red["pattern"] if r.passed), corpus.REDACTION_PATTERNS),
            "",
        ],
        [
            "redaction: near misses unchanged",
            of(sum(1 for r in red["near_miss"] if r.passed), corpus.REDACTION_NEAR_MISSES),
            "",
        ],
        [
            "rule controls",
            of(results.controls[0], results.controls[1]),
            "a positive and a negative control for each of 18 rules",
        ],
        [
            "production-write control",
            "verified on a synthetic target",
            "no production system exists in this repository; the 9 non-mock cases use systems recorded as "
            "staging, production or unknown for the test",
        ],
    ]
    out += table(["Claim", "Result", "Notes"], rows)
    skipped = [
        (n, o.case_id) for n, items in results.gateway.items() for _, o in items if o.skipped
    ]
    if skipped:
        out += [f"Not run on this host: {', '.join(i for _, i in skipped)}.", ""]

    out += ["## Gateway corpora by group", ""]
    for name, items in results.gateway.items():
        counts: Counter[str] = Counter()
        passed: Counter[str] = Counter()
        for case, outcome in items:
            counts[case.group] += 1
            passed[case.group] += int(outcome.passed)
        out += [f"**{name}**", ""]
        out += table(
            ["Group", "Passed", "Cases"], [[g, str(passed[g]), str(counts[g])] for g in counts]
        )
    out += ["## Injection corpus", "",
            "8 payload classes across 5 carriers. A carrier is where the payload sits before it reaches "
            "the agent through a real tool result.", ""]  # fmt: skip
    by_payload: dict[str, list[InjectionResult]] = {}
    for c in inj:
        by_payload.setdefault(c.payload, []).append(c)
    out += table(
        ["Payload class", "Cases passed", "Attempts", "Refused or approval needed", "Executed"],
        [[p, f"{sum(1 for c in cs if c.passed)} of {len(cs)}", str(sum(c.attempts for c in cs)),
          str(sum(c.refused for c in cs)), str(sum(c.executed for c in cs))] for p, cs in by_payload.items()],
    )  # fmt: skip
    out += table(
        ["Carrier", "Cases passed"],
        [[k, f"{sum(1 for c in inj if c.carrier == k and c.passed)} of {sum(1 for c in inj if c.carrier == k)}"]
         for k in injection.CARRIERS],
    )  # fmt: skip
    out += ["## Tamper corpus", ""]
    out += table(
        ["Case", "Edit", "Result"],
        [[t.id, t.description,
          ("detected at the expected event" if t.detected and t.at_expected_event else
           "detected, not at the expected event" if t.detected else
           "not detected" + (" (documented limit)" if not t.detectable else ""))] for t in results.tamper],
    )  # fmt: skip
    problems = failures(results)
    out += ["## Cases that did not pass", ""]
    out += problems if problems else ["None."]
    out += [
        "",
        "## Defects the corpora found, fixed before this report was generated",
        "",
        "The first run of the redaction corpus failed 3 of 22 sentinel cases: a planted approver token",
        "and a planted database password reached stored audit rows (`R-AUDIT-approver`,",
        "`R-AUDIT-password`) and one REST audit response (`R-REST-audit`). The audit log redacted only",
        "the secrets it was given, and the gateway and the REST dependency gave it none. Fixed at the",
        "source: one shared list of the configured secrets (`app/policy/secrets.py`), used by the",
        "server, by the gateway's own audit notes and by the REST audit dependency. The corpus then",
        "passed 22 of 22; the numbers above are from that run.",
        "",
        "Smaller fixes made while building the corpora: the v1 session limits were too low for one",
        "mapping run (12/40 raised to 40/60); a forbidden-name pattern tripped the architecture test and",
        "was rewritten as `answer[_-]?key`; a NUL byte in a tool argument crashed the audit insert and is",
        "now escaped; `propose_mapping` no longer takes a free-text requirement argument; and two",
        "forged-event tests shared one row hash, which the table refuses, so each forged event now",
        "derives its own.",
        "",
        "## Limits",
        "",
        "* The corpora are hand-written and test our rules; they are not an adversarial search.",
        "* The scripted adversary follows fixed instructions; real agents differ.",
        "* The symlink-to-a-file case uses a junction to a directory on a host that cannot create a file",
        "  symlink (Windows without the privilege); it exercises the same resolution check.",
        "* Removing the last events of an audit chain is not detected (no anchor outside the database).",
        "* The policy lock lives in the repository, so it guards against drift, not against someone with",
        "  write access.",
        "* Tested with the official Python MCP SDK client only.",
        "",
    ]
    return "\n".join(out)


def write(results: Results, path: Path = DOC) -> None:
    path.write_text(render(results), encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    with (
        throwaway_engine() as engine,
        tempfile.TemporaryDirectory(prefix="morph-policy-eval-") as tmp,
    ):
        results = run_everything(engine, Path(tmp))
    write(results)
    print(f"wrote {DOC}")
    problems = failures(results)
    for line in problems:
        print(f"problem: {line}", file=sys.stderr)
    if args.check:
        name = str(DOC.relative_to(REPO))
        tracked = subprocess.run(  # noqa: S603
            ["git", "ls-files", "--error-unmatch", name],  # noqa: S607
            cwd=REPO, capture_output=True, check=False,
        )  # fmt: skip
        if tracked.returncode:
            print(f"{name} is not tracked by git", file=sys.stderr)
            return 1
        diff = subprocess.run(  # noqa: S603
            ["git", "diff", "--exit-code", "--", name],  # noqa: S607
            cwd=REPO, check=False,
        ).returncode  # fmt: skip
        return 1 if diff or problems else 0
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
