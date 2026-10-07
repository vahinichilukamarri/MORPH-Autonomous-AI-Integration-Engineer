"""O8: the review gate. No sandbox needed: generation must refuse unreviewed required mappings."""

import pytest
from app.codegen.sandbox import SandboxRunner
from app.mapping.confidence import ReviewStatus
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from morph_bench.oracle.build import build_integration
from morph_bench.oracle.models import load_fixture
from morph_bench.oracle.recorder import Checks
from tests.conftest import test_engine  # noqa: F401

SCENARIOS = {
    "S1": ("crm_customer_to_support_user", "tier"),
    "S4": ("crm_v2_to_support_v2", "serviceTier"),
}


@pytest.mark.docker
@pytest.mark.parametrize("scenario", list(SCENARIOS), ids=list(SCENARIOS))
def test_o8_a_required_mapping_under_review_blocks_generation(
    scenario: str,
    test_engine: Engine,  # noqa: F811
) -> None:
    scenario_id, field = SCENARIOS[scenario]
    fixture = load_fixture(scenario_id)
    with Session(test_engine) as session, Checks(scenario_id, "O8") as c:
        version = build_integration(
            session, fixture, SandboxRunner(), statuses={field: ReviewStatus.NEEDS_REVIEW}
        )
        blocking = (version.manifest.get("review") or {}).get("blocking", [])
        c.ok(
            "status_is_blocked_pending_review",
            version.status == "BLOCKED_PENDING_REVIEW",
            version.status,
        )
        c.ok("no_code_was_produced", version.files == [] and version.bundle_hash is None, "")
        c.ok(
            f"the_reason_names_{field}",
            [b["target_field"] for b in blocking] == [field],
            f"{blocking}",
        )
        c.ok(
            "the_reason_says_review",
            all(b["reason"] == "NEEDS_REVIEW" for b in blocking),
            f"{blocking}",
        )


@pytest.mark.docker
def test_o8_s3_is_blocked_until_a_human_decides_the_unresolved_field(
    test_engine: Engine,  # noqa: F811
) -> None:
    fixture = load_fixture("support_user_to_crm_customer")
    with Session(test_engine) as session, Checks(fixture.scenario, "O8") as c:
        blocked = build_integration(session, fixture, SandboxRunner(), with_override=False)
        blocking = (blocked.manifest.get("review") or {}).get("blocking", [])
        c.ok(
            "without_the_override_generation_is_blocked",
            blocked.status == "BLOCKED_PENDING_REVIEW" and blocked.files == [],
            blocked.status,
        )
        c.ok(
            "the_reason_names_segment_as_unresolved",
            [(b["target_field"], b["reason"]) for b in blocking] == [("segment", "UNRESOLVED")],
            f"{blocking}",
        )
        ready = build_integration(session, fixture, SandboxRunner(), with_override=True)
        c.ok("with_the_human_decision_it_is_ready", ready.status == "READY", ready.status)
        mapping = next(
            m for m in ready.manifest["mapping_versions"] if m["target_field"] == "segment"
        )
        c.ok(
            "the_human_decision_is_recorded_as_a_human_version",
            mapping["author"] == "human"
            and mapping["review_status"] == "OVERRIDDEN"
            and mapping["version"] == 2,
            f"{mapping}",
        )
