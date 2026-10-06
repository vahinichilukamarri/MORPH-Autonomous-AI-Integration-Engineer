# ruff: noqa: E501
from morph_bench.grader import FieldGrade
from morph_bench.mapping_eval import (
    FieldRecord,
    Metrics,
    ReportMeta,
    RunRecord,
    lossy_breakdown,
    render_report,
)


def field(
    expected: str, proposed: str, flagged: bool, lossy: tuple[str, ...] = (), name: str = "f"
) -> FieldRecord:
    grade = FieldGrade(
        target_field=name,
        expected_type=expected,
        proposed_type=proposed,
        source_match=None,
        type_match=expected == proposed,
        transformation_correct=None,
        unresolved_correct=None,
        flag_correct=None,
        guessed=expected == "UNRESOLVED" and proposed != "UNRESOLVED",
        fully_correct=expected == "UNRESOLVED" and proposed == "UNRESOLVED",
        flagged=flagged,
        confidence=None,
        invalid_output=False,
        detail="",
    )
    return FieldRecord(
        grade, proposed, expected, "NEEDS_REVIEW" if flagged else "AUTO_ACCEPTED", "WARN", lossy
    )


def record(config: str, *fields: FieldRecord) -> RunRecord:
    return RunRecord(
        "v1/confidence-v1", "s", config, 1, "p", "m", tuple(fields), Metrics(len(fields))
    )


def test_breakdown_separates_declined_flagged_guesses_and_silent_guesses() -> None:
    result = lossy_breakdown(
        [
            record(
                "B",
                field("UNRESOLVED", "UNRESOLVED", True),  # declined
                field("UNRESOLVED", "DERIVED", True),  # proposed, flagged
                field("UNRESOLVED", "DERIVED", False),  # silent guess
                field("UNRESOLVED", "DIRECT", False),  # silent guess
                field("DERIVED", "DERIVED", True, ("INFORMATION_LOSS_ENUM",)),
                field("DERIVED", "DERIVED", False, ("INFORMATION_LOSS_COALESCE",)),
                field("DIRECT", "DIRECT", False),
            )
        ]
    )
    assert (result.unrecoverable, result.declined) == (4, 1)
    assert (result.guessed_flagged, result.guessed_silent) == (1, 2)
    assert (result.lossy_proposals, result.lossy_flagged, result.lossy_silent) == (2, 1, 1)


def test_a_declined_field_is_not_counted_as_a_lossy_proposal() -> None:
    result = lossy_breakdown(
        [record("B", field("UNRESOLVED", "UNRESOLVED", True, ("INFORMATION_LOSS_ENUM",)))]
    )
    assert result.lossy_proposals == 0


def test_report_shows_the_section_with_computed_shares() -> None:
    records = [
        record(
            "B",
            field("UNRESOLVED", "UNRESOLVED", True),
            field("UNRESOLVED", "DERIVED", True),
            field("UNRESOLVED", "DERIVED", False),
            field("DERIVED", "DERIVED", True, ("INFORMATION_LOSS_ENUM",)),
        )
    ]
    text = render_report(records, ReportMeta("d", False, 1))
    section = text.split("### Lossy mappings: flagged versus silent")[1].split("### Confidence")[0]
    assert "Informational only" in section and "silent guess" in section
    assert (
        "| B: LLM, full schema | 1/3 (33.3%) | 1/3 (33.3%) | 1/3 (33.3%) | 1/1 (100.0%) | 0/1 (0.0%) |"
        in section
    )


def test_old_records_without_lossy_codes_still_load() -> None:
    rec = record("B", field("DIRECT", "DIRECT", False))
    import json

    raw = json.loads(rec.to_json())
    for f in raw["fields"]:
        del f["lossy_codes"]
    assert RunRecord.from_json(json.dumps(raw)).fields[0].lossy_codes == ()
