"""Validator v2 post-hoc rescoring of saved v1 results. Pure: no DB, no LLM.

Everything here is labelled "v2, post-hoc, designed after seeing v1 results". The v1 evaluation is
reported unchanged above this section; the v2 checks were written after its failures were known, so
the comparison is not an unbiased estimate of how v2 would do on new data.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from morph_bench.mapping_eval import BUCKETS, bucket_label, fraction

START = "<!-- v2-post-hoc:start -->"
END = "<!-- v2-post-hoc:end -->"
NEEDS_REVIEW = "NEEDS_REVIEW"
LLM_CONFIGS = ("B", "C")
CONFIG_TITLES = {"B": "B: LLM, full schema", "C": "C: LLM + retrieval (RAG)"}
POLICIES = (
    ("v1", "v1 (as reported above)"),
    ("v2", "v2 as shipped (null and constant checks force review; lossy checks informational)"),
    ("v2_truncation", "v2 + LOSSY_TRUNCATION forces review"),
    (
        "v2_collapse",
        "v2 + many-to-one value collapse forces review (INFORMATION_LOSS_ENUM, LOSSY_COLLAPSE)",
    ),
    ("v2_all_lossy", "v2 + every lossy warning forces review"),
)


@dataclass(frozen=True)
class FieldPair:
    """One mapping, scored by validator v1 and by validator v2 from the same saved response."""

    scenario_id: str
    config: str
    target_field: str
    fully_correct: bool
    unresolved: bool
    proposed: str
    expected: str
    v1_confidence: float | None
    v2_confidence: float | None
    v1_codes: tuple[str, ...]
    v2_codes: tuple[str, ...]
    reviews: dict[str, str]  # policy key -> AUTO_ACCEPTED or NEEDS_REVIEW
    confidence: dict[str, float | None] = field(default_factory=dict)  # policy key -> score
    detail: str = ""  # why the grader scored it wrong

    @property
    def new_codes(self) -> tuple[str, ...]:
        return tuple(c for c in self.v2_codes if c not in self.v1_codes)

    def flagged(self, policy: str) -> bool:
        return self.reviews[policy] == NEEDS_REVIEW


@dataclass(frozen=True)
class V2Meta:
    run_date: str
    model: str
    responses_reused: int
    llm_calls_made: int
    v1_reproduced: int  # fields whose v1 outcome was reproduced exactly from the saved responses
    fields_checked: int


def _of(pairs: Iterable[FieldPair], config: str) -> list[FieldPair]:
    return [p for p in pairs if p.config == config]


def accuracy(pairs: Sequence[FieldPair]) -> str:
    return fraction(sum(p.fully_correct for p in pairs), len(pairs))


def flagged_rate(pairs: Sequence[FieldPair], policy: str) -> str:
    return fraction(sum(p.flagged(policy) for p in pairs), len(pairs))


def unflagged(pairs: Sequence[FieldPair], policy: str) -> list[FieldPair]:
    return [p for p in pairs if not p.flagged(policy)]


def flagged(pairs: Sequence[FieldPair], policy: str) -> list[FieldPair]:
    return [p for p in pairs if p.flagged(policy)]


def newly_flagged(pairs: Sequence[FieldPair], policy: str) -> list[FieldPair]:
    return [p for p in pairs if p.flagged(policy) and not p.flagged("v1")]


def bucket_rows(pairs: Sequence[FieldPair], policy: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for low, high in BUCKETS:
        inside = [p for p in pairs if (c := p.confidence[policy]) is not None and low <= c < high]
        rows.append(
            (bucket_label(low, high), fraction(sum(p.fully_correct for p in inside), len(inside)))
        )
    none = [p for p in pairs if p.confidence[policy] is None]
    rows.append(
        ("no confidence (UNRESOLVED)", fraction(sum(p.fully_correct for p in none), len(none)))
    )
    return rows


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return [*lines, *("| " + " | ".join(r) + " |" for r in rows), ""]


def _label(p: FieldPair) -> str:
    return f"`{p.scenario_id}` `{p.target_field}`"


def render_v2_section(pairs: Sequence[FieldPair], meta: V2Meta) -> str:
    out = [
        START,
        "",
        "## Validator v2, post-hoc rescoring",
        "",
        "> **v2, post-hoc, designed after seeing v1 results.** The v2 checks were written",
        "> after the v1 failures above were known, and they are aimed at exactly those failures.",
        "> This section is therefore **not an unbiased result**: it shows what the new checks",
        "> would have done to this run, not how they will do on new scenarios. The v1 section",
        "> above is unchanged.",
        "",
        f"- Rescored on {meta.run_date} from the saved v1 responses of `{meta.model}`: "
        f"{meta.responses_reused} stored responses reused, "
        f"**{meta.llm_calls_made} new LLM calls**.",
        f"- Reproduction check: validator v1 re-run on the same saved responses reproduced "
        f"{meta.v1_reproduced}/{meta.fields_checked} mapping outcomes exactly (correctness, review "
        "status and confidence), so the comparison below starts from the reported v1 numbers.",
        "- Accuracy means *fully correct* as graded against the answer keys. The mappings are "
        "identical in v1 and v2; only validation, confidence and review flags differ.",
        "",
        "### What v2 adds",
        "",
        "| Check | Rule | Effect |",
        "|---|---|---|",
        "| `MOSTLY_NULL_OUTPUT` | more than half of the sample records with a non-null source "
        "produce null | review-forcing warning |",
        "| `CONSTANT_OUTPUT` | a mapping not declared CONSTANT gives one single value although "
        "the sources differ | review-forcing warning |",
        "| `LOSSY_COLLAPSE` | several distinct values of a categorical source give the same output "
        "(beyond the v1 `MAP_ENUM` check) | recorded, does not force review |",
        "| `LOSSY_TRUNCATION` | a `SPLIT_PART` discards parts of the input for some record | "
        "recorded, does not force review |",
        "",
        "A review-forcing warning also lowers validation from PASS to WARN, so confidence drops "
        "(formula v1, unchanged). The two lossy checks do not force review; the reasoning and the "
        "measured trade-off are in the policy table below.",
        "",
    ]
    for config in LLM_CONFIGS:
        subset = _of(pairs, config)
        if not subset:
            continue
        out += _config_section(config, subset)
    out += _policy_section(pairs)
    out += [END, ""]
    return "\n".join(out)


def _config_section(config: str, pairs: Sequence[FieldPair]) -> list[str]:
    title = CONFIG_TITLES[config]
    v1_unflagged, v2_unflagged = unflagged(pairs, "v1"), unflagged(pairs, "v2")
    v1_flagged, v2_flagged = flagged(pairs, "v1"), flagged(pairs, "v2")
    rows = [
        ["Flagged for review", flagged_rate(pairs, "v1"), flagged_rate(pairs, "v2")],
        ["Accuracy of flagged mappings", accuracy(v1_flagged), accuracy(v2_flagged)],
        [
            "Accuracy of unflagged (auto-accepted) mappings",
            accuracy(v1_unflagged),
            accuracy(v2_unflagged),
        ],
        [
            "Wrong mappings that were auto-accepted",
            fraction(
                sum(not p.fully_correct for p in v1_unflagged),
                sum(not p.fully_correct for p in pairs),
            ),
            fraction(
                sum(not p.fully_correct for p in v2_unflagged),
                sum(not p.fully_correct for p in pairs),
            ),
        ],
    ]
    out = [
        f"### {title}: v1 versus v2",
        "",
        *_table(["Measure", "v1", "v2"], rows),
        "Confidence bucket versus accuracy (fully correct fraction):",
        "",
    ]
    v1_b, v2_b = bucket_rows(pairs, "v1"), bucket_rows(pairs, "v2")
    out += _table(
        ["Confidence", "v1", "v2"], [[a, x, y] for (a, x), (_, y) in zip(v1_b, v2_b, strict=True)]
    )
    new = newly_flagged(pairs, "v2")
    caught = [p for p in new if not p.fully_correct]
    false_positives = [p for p in new if p.fully_correct]
    out += [
        f"Newly flagged by v2: {len(new)} mapping(s), of which {len(caught)} were wrong "
        f"(caught) and {len(false_positives)} were correct (new false positives).",
        "",
    ]
    if new:
        out += _table(
            ["Scenario / field", "Correct?", "Proposed", "New v2 codes", "v1 to v2 confidence"],
            [
                [
                    _label(p),
                    "yes (false positive)" if p.fully_correct else "no (caught)",
                    p.proposed,
                    ", ".join(p.new_codes) or "(lower confidence only)",
                    f"{_c(p.v1_confidence)} to {_c(p.v2_confidence)}",
                ]
                for p in new
            ],
        )
    informational = [p for p in pairs if p.new_codes and not p.flagged("v2")]
    if informational:
        out += ["Recorded by v2 but not forced to review (informational):", ""]
        out += _table(
            ["Scenario / field", "Correct?", "New v2 codes"],
            [
                [_label(p), "yes" if p.fully_correct else "no", ", ".join(p.new_codes)]
                for p in informational
            ],
        )
    silent = [p for p in v2_unflagged if not p.fully_correct]
    out += [f"Wrong mappings still auto-accepted under v2: {len(silent)}.", ""]
    if silent:
        out += _table(
            ["Scenario / field", "Proposed", "Expected", "Why it is wrong"],
            [[_label(p), p.proposed, p.expected, p.detail or "-"] for p in silent],
        )
    return out


def _c(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _policy_section(pairs: Sequence[FieldPair]) -> list[str]:
    out = [
        "### Should the lossy checks force review?",
        "",
        "Measured on this run, for B and C together (64 mapping outcomes). *Caught* is a wrong "
        "mapping that this policy sends to review but v1 did not; *false positive* is a correct "
        "mapping that this policy sends to review but v1 did not.",
        "",
    ]
    rows: list[list[str]] = []
    llm = [p for p in pairs if p.config in LLM_CONFIGS]
    for key, title in POLICIES:
        new = [p for p in llm if p.flagged(key) and not p.flagged("v1")]
        un = unflagged(llm, key)
        rows.append(
            [
                title,
                flagged_rate(llm, key),
                accuracy(un),
                str(sum(not p.fully_correct for p in new)),
                str(sum(p.fully_correct for p in new)),
                str(sum(not p.fully_correct for p in un)),
            ]
        )
    out += _table(
        [
            "Policy",
            "Flagged for review",
            "Accuracy of unflagged",
            "Caught",
            "False positives",
            "Wrong and still unflagged",
        ],
        rows,
    )
    counts = Counter(c for p in llm for c in p.new_codes)
    truncated = [p for p in llm if "LOSSY_TRUNCATION" in p.v2_codes]
    collapsed = [p for p in llm if {"INFORMATION_LOSS_ENUM", "LOSSY_COLLAPSE"} & set(p.v2_codes)]
    out += [
        "New v2 codes raised across B and C: "
        + (", ".join(f"{code} x{n}" for code, n in sorted(counts.items())) or "none")
        + ".",
        "",
        "The *false positives* column only counts correct mappings that v1 had not already sent to "
        "review. How many correct mappings carry each lossy code, and how many of those v1 already "
        "flagged:",
        "",
        *_table(
            [
                "Lossy code family",
                "Mappings carrying it",
                "Correct",
                "Correct and already flagged in v1",
            ],
            [
                [
                    "LOSSY_TRUNCATION",
                    str(len(truncated)),
                    str(sum(p.fully_correct for p in truncated)),
                    str(sum(p.fully_correct and p.flagged("v1") for p in truncated)),
                ],
                [
                    "INFORMATION_LOSS_ENUM / LOSSY_COLLAPSE",
                    str(len(collapsed)),
                    str(sum(p.fully_correct for p in collapsed)),
                    str(sum(p.fully_correct and p.flagged("v1") for p in collapsed)),
                ],
            ],
        ),
        "### Lossy checks: shipped informational, decision left to you",
        "",
        "As shipped, no lossy warning forces review (`LOSSY_FORCES_REVIEW` is empty), because "
        "this run alone cannot justify it for every kind of loss. What the numbers above say:",
        "",
        "- Truncation (`SPLIT_PART` discarding parts of the input) is the only lossy signal that "
        "touched a wrong mapping here. Forcing it would have sent the silent `last_name` error to "
        "review. The correct mappings that carry it were already in review in v1, so on this run "
        "it adds no new false positive; on other data a correct split would be flagged, which is "
        "acceptable only because the pipeline really does discard user data.",
        "- A many-to-one value collapse (the `tier` map, SMB and MIDMARKET to STANDARD) is "
        "intended by the business rule in the requirement text and was correct in every case "
        "here. Forcing review for it turns each of those correct mappings into a false positive, "
        "and no wrong mapping carried the code. A deterministic check cannot tell an intended "
        "collapse from an accidental one.",
        "- So the reasoning favours forcing review for truncation only, and not for value "
        "collapse. That is a recommendation, **not applied**; it needs your decision.",
        "",
        "### Limits of this section",
        "",
        "- The checks were designed after the failures were seen, and the sample is 32 fields per "
        "configuration at N=1. Counts of one or two mappings are within noise.",
        "- v2 only helps when the failure shows up in the sample records: a wrong mapping that "
        "produces plausible, varied, non-null output (the `customer_id` built from `userId` in "
        "B) is invisible to these checks and is flagged only by the confidence threshold.",
        "- Two things v2 does not fix: the per-field confidence still comes from the unchanged v1 "
        "formula, and v2 never inspects whether a mapping is semantically right, only whether its "
        "outputs on the sample records look degenerate.",
        "",
    ]
    return out


def splice_section(existing: str, section: str) -> str:
    """The existing document with the v2 section appended or replaced; the text above is kept."""
    before = existing.split(START, 1)[0].rstrip("\n")
    return before + "\n\n" + section
