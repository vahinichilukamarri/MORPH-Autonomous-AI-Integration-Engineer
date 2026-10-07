"""Persistence, versioning and the review gate end to end, on the throwaway test database."""

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.codegen.service import (
    BLOCKED_PENDING_REVIEW,
    BLOCKED_UNSUPPORTED,
    GENERATED,
    generate,
    materialize,
)
from app.db_models import Integration, IntegrationVersion
from app.mapping.confidence import ReviewStatus
from tests.codegen.fixtures import (
    S1_PIPELINES,
    S3_PIPELINES,
    S3_SEGMENT_OVERRIDE,
    approve_all,
    mapped,
    persist_run,
)


def s1_run(session: Session, **overrides: object) -> int:
    return persist_run(
        session, source=("crm.v1", "crm"), target=("support.v1", "support"),
        source_entity="Customer", target_entity="User",
        fields=mapped(S1_PIPELINES, **overrides),  # type: ignore[arg-type]
    )  # fmt: skip


def s3_run(session: Session) -> int:
    return persist_run(
        session, source=("support.v1", "support"), target=("crm.v1", "crm"),
        source_entity="User", target_entity="Customer",
        fields=mapped(S3_PIPELINES, segment=(ReviewStatus.NEEDS_REVIEW, None)),
    )  # fmt: skip


def test_generation_persists_files_manifest_and_gate(session: Session, tmp_path: Path) -> None:
    version = generate(session, s1_run(session))
    assert version.status == GENERATED and version.version == 1
    assert {f.path for f in version.files} >= {
        "integration/transform.py",
        "integration/strategy.py",
    }
    assert [(g.stage, g.passed) for g in version.gate_results] == [("ast", True)]
    manifest = version.manifest
    assert (
        manifest["review"]["gate_status"] == "OPEN"
        and manifest["bundle_hash"] == version.bundle_hash
    )
    assert manifest["source"]["spec_hash"] and manifest["target"]["entity"] == "User"
    assert all(m["version"] == 1 for m in manifest["mapping_versions"])
    root = materialize(version, tmp_path / "bundle")
    assert (root / "manifest.json").is_file() and (root / "integration" / "__main__.py").is_file()


def test_regenerating_with_the_same_inputs_reuses_the_version(session: Session) -> None:
    run_id = s1_run(session)
    first = generate(session, run_id)
    second = generate(session, run_id)
    assert second.id == first.id
    assert session.scalar(select(func.count()).select_from(IntegrationVersion)) == 1


def test_s3_is_blocked_pending_review_until_a_human_decides(session: Session) -> None:
    run_id = s3_run(session)
    blocked = generate(session, run_id)
    assert blocked.status == BLOCKED_PENDING_REVIEW and not blocked.files
    assert [b["target_field"] for b in blocked.manifest["review"]["blocking"]] == ["segment"]
    assert blocked.bundle_hash is None
    approve_all(session, run_id, "segment", S3_SEGMENT_OVERRIDE)
    ready = generate(session, run_id)
    assert ready.status == GENERATED and ready.version == 2 and ready.id != blocked.id
    segment = next(m for m in ready.manifest["mapping_versions"] if m["target_field"] == "segment")
    assert (segment["version"], segment["author"], segment["review_status"]) == (
        2, "human", "OVERRIDDEN",
    )  # fmt: skip
    assert session.scalar(select(func.count()).select_from(Integration)) == 1  # one integration


def test_unsupported_plans_are_recorded_not_raised(session: Session) -> None:
    run_id = persist_run(
        session, source=("crm.v1", "crm"), target=("crm.v1", "crm"),
        source_entity="Customer", target_entity="CustomerPage", fields=mapped(S1_PIPELINES),
    )  # fmt: skip
    version = generate(session, run_id)
    assert version.status == BLOCKED_UNSUPPORTED
    assert "NO_TARGET_LOOKUP" in version.manifest["plan_error"]
