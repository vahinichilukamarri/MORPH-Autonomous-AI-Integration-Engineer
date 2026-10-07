"""Policy v2.1 post-hoc comparison (v1 / v2 / v2.1) of the saved v1 responses. Pure: no DB, no LLM.

v2.1 changes one policy decision of validator v2: ``LOSSY_TRUNCATION`` now forces review, while
``LOSSY_COLLAPSE`` stays a visible warning. It was chosen after the v1 failures and the v2
rescoring were seen, so it is labelled post-hoc and is not an unbiased result.
"""

from collections.abc import Sequence

from morph_bench.mapping_eval import fraction
from morph_bench.rescore_v2 import (
    CONFIG_TITLES,
    LLM_CONFIGS,
    FieldPair,
    V2Meta,
    flagged,
    unflagged,
)

START = "<!-- v2.1-post-hoc:start -->"
END = "<!-- v2.1-post-hoc:end -->"
POLICIES = (("v1", "v1"), ("v2", "v2"), ("v2_1", "v2.1"))


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return [*lines, *("| " + " | ".join(r) + " |" for r in rows), ""]


def _acc(pairs: Sequence[FieldPair]) -> str:
    return fraction(sum(p.fully_correct for p in pairs), len(pairs))


def _wrong_auto(pairs: Sequence[FieldPair], policy: str) -> str:
    wrong = [p for p in pairs if not p.fully_correct]
    return fraction(sum(not p.flagged(policy) for p in wrong), len(wrong))


def comparison_rows(pairs: Sequence[FieldPair]) -> list[list[str]]:
    rows: list[list[str]] = []
    measures: list[tuple[str, list[str]]] = [
        ("Flagged for review", [fraction(len(flagged(pairs, k)), len(pairs)) for k, _ in POLICIES]),
        (
            "Accuracy of unflagged (auto-accepted) mappings",
            [_acc(unflagged(pairs, k)) for k, _ in POLICIES],
        ),
        ("Accuracy of flagged mappings", [_acc(flagged(pairs, k)) for k, _ in POLICIES]),
        ("Wrong mappings that were auto-accepted", [_wrong_auto(pairs, k) for k, _ in POLICIES]),
    ]
    for label, cells in measures:
        rows.append([label, *cells])
    new_fp = ["0"]
    caught = ["0"]
    for key, _ in POLICIES[1:]:
        newly = [p for p in pairs if p.flagged(key) and not p.flagged("v1")]
        new_fp.append(str(sum(p.fully_correct for p in newly)))
        caught.append(str(sum(not p.fully_correct for p in newly)))
    rows.append(["New false positives (correct mappings flagged now, not in v1)", *new_fp])
    rows.append(["Wrong mappings newly caught (flagged now, not in v1)", *caught])
    return rows


def _label(p: FieldPair) -> str:
    return f"`{p.scenario_id}` `{p.target_field}`"


def render_v21_section(pairs: Sequence[FieldPair], meta: V2Meta) -> str:
    out = [
        START,
        "",
        "## Policy v2.1, post-hoc rescoring",
        "",
        "> **v2.1, post-hoc, chosen after seeing v1 failures.** The decision below was made after",
        "> the v1 results and the v2 rescoring above had been reviewed, and it is aimed at a",
        "> failure that was already known. It is therefore **not an unbiased result**: it shows",
        "> what the policy would have done to this run, not how it will do on new scenarios.",
        "> The v1 and v2 sections above are unchanged.",
        "",
        "**What changed (policy only, the checks are the same as v2):**",
        "",
        "- `LOSSY_TRUNCATION` now forces `NEEDS_REVIEW` (`LOSSY_FORCES_REVIEW`).",
        "- `LOSSY_COLLAPSE` (and the v1 `INFORMATION_LOSS_ENUM`) stay visible warnings and "
        "do **not** force review.",
        "- Everything else, including the confidence formula (v1), is as in v2.",
        "",
        f"Rescored on {meta.run_date} from the saved responses of `{meta.model}`: "
        f"{meta.responses_reused} stored responses reused, "
        f"**{meta.llm_calls_made} new LLM calls**; "
        f"validator v1 reproduced {meta.v1_reproduced}/{meta.fields_checked} outcomes exactly "
        "before v2 and v2.1 were applied.",
        "",
    ]
    for config in LLM_CONFIGS:
        subset = [p for p in pairs if p.config == config]
        if subset:
            out += [f"### {CONFIG_TITLES[config]}: v1 versus v2 versus v2.1", ""]
            out += _table(["Measure", "v1", "v2", "v2.1"], comparison_rows(subset))
    both = [p for p in pairs if p.config in LLM_CONFIGS]
    out += ["### B and C together", ""]
    out += _table(["Measure", "v1", "v2", "v2.1"], comparison_rows(both))

    new = [p for p in both if p.flagged("v2_1") and not p.flagged("v2")]
    out += [
        f"Flagged by v2.1 but not by v2: {len(new)} mapping(s), "
        f"{sum(not p.fully_correct for p in new)} wrong and "
        f"{sum(p.fully_correct for p in new)} correct.",
        "",
    ]
    if new:
        out += _table(
            ["Scenario / field", "Config", "Correct?", "Proposed", "Codes"],
            [
                [
                    _label(p),
                    p.config,
                    "yes (false positive)" if p.fully_correct else "no (caught)",
                    p.proposed,
                    ", ".join(c for c in p.v2_codes if c not in p.v1_codes) or "-",
                ]
                for p in new
            ],
        )
    out += _limitations(both)
    out += [END, ""]
    return "\n".join(out)


def _limitations(pairs: Sequence[FieldPair]) -> list[str]:
    invisible = [p for p in pairs if not p.fully_correct and not p.unresolved and not p.new_codes]
    out = [
        "### Known limitations (not fixed)",
        "",
        "- **B's fabricated `customer_id` is not detectable by any deterministic check of its "
        "outputs.** The model built it from `userId` (cast to string, prefixed `C-`), which "
        "gives plausible, varied, non-null output that satisfies the target schema, so no "
        "validator check on the sample outputs (v1 or v2) can tell it from a genuine id. In v1 "
        "it was still sent to review, but not because of its output: the model itself listed "
        "`externalRef` among its rejected alternatives, which the v1 ambiguity rule treats as a "
        "reason for review and which also lowers the confidence score (0.54). v2 and v2.1 do not "
        "change that, and no deterministic fix is known. It is recorded here as a known "
        "limitation, not fixed.",
    ]
    out += [
        "- Wrong mappings with no v2 finding at all in this run, and how they were flagged:",
        "",
        *_table(
            [
                "Scenario / field",
                "Config",
                "v1 validation codes",
                "v1 confidence",
                "Flagged by v1",
                "Flagged by v2.1",
            ],
            [
                [
                    _label(p),
                    p.config,
                    ", ".join(p.v1_codes) or "none",
                    "-" if p.v1_confidence is None else f"{p.v1_confidence:.2f}",
                    "yes" if p.flagged("v1") else "no",
                    "yes" if p.flagged("v2_1") else "no",
                ]
                for p in invisible
            ],
        ),
    ]
    out += [
        "- The sample is 32 fields per configuration at N=1; differences of one or two mappings "
        "are within noise.",
        "- Truncation forcing flags any `SPLIT_PART` that discards user data, including correct "
        "ones (the first-token `first_name`); on this run those were already in review.",
        "",
    ]
    return out
