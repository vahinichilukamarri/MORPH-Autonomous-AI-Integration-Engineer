"""Codegen test inputs built from the committed mock OpenAPI specs and inline pipelines."""

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.codegen.inputs import CodegenInput, MappedField
from app.db_models import Mapping, MappingRun, MappingVersion
from app.discovery.parser import parse_spec
from app.discovery.repository import ingest
from app.mapping.confidence import ReviewStatus
from app.mapping.proposal import MappingType
from app.mapping.samples import load_samples
from app.mapping.transform import Transformation

ROOT = Path(__file__).resolve().parents[3] / "mock_systems" / "openapi"
SAMPLES = Path(__file__).resolve().parents[3] / "mock_systems" / "samples"


def model(file: str, name: str):  # type: ignore[no-untyped-def]
    return parse_spec(json.loads((ROOT / f"{file}.json").read_text(encoding="utf-8")), name)


def pipeline(*steps: dict[str, Any]) -> Transformation:
    return Transformation.model_validate({"steps": list(steps)})


def copy(field: str) -> dict[str, Any]:
    return {"op": "COPY", "field": field}


def enum(mapping: dict[str, str]) -> dict[str, Any]:
    return {"op": "MAP_ENUM", "mapping": mapping}


S1_PIPELINES: dict[str, Transformation] = {
    "userId": pipeline(
        copy("customer_id"),
        {"op": "REGEX_EXTRACT", "pattern": "^C-(\\d+)$", "group": 1},
        {"op": "CAST", "to": "int"},
    ),
    "externalRef": pipeline(copy("customer_id")),
    "fullName": pipeline(
        {
            "op": "JOIN_NONNULL",
            "fields": ["first_name", "last_name"],
            "separator": " ",
            "trim": True,
        }
    ),
    "email_address": pipeline(copy("email")),
    "phoneNumber": pipeline(copy("phone"), {"op": "STRIP_PREFIX", "prefix": "+"}),
    "accountState": pipeline(
        copy("status"), enum({"ACTIVE": "ENABLED", "INACTIVE": "DISABLED", "SUSPENDED": "BLOCKED"})
    ),
    "tier": pipeline(
        copy("segment"),
        enum({"SMB": "STANDARD", "MIDMARKET": "STANDARD", "ENTERPRISE": "PRIORITY"}),
    ),
    "createdAt": pipeline(
        copy("created_at"),
        {"op": "FORMAT_DATETIME", "from_format": "iso8601", "to_format": "epoch_s"},
    ),
}

S3_PIPELINES: dict[str, Transformation] = {
    "customer_id": pipeline(copy("externalRef")),
    "first_name": pipeline(copy("fullName"), {"op": "SPLIT_PART", "separator": " ", "index": 0}),
    "last_name": pipeline(
        copy("fullName"), {"op": "REGEX_EXTRACT", "pattern": "^\\S+ (.+)$", "group": 1}
    ),
    "email": pipeline(copy("email_address")),
    "phone": pipeline(copy("phoneNumber"), {"op": "ADD_PREFIX", "prefix": "+"}),
    "status": pipeline(
        copy("accountState"),
        enum({"ENABLED": "ACTIVE", "DISABLED": "INACTIVE", "BLOCKED": "SUSPENDED"}),
    ),
    "created_at": pipeline(
        copy("createdAt"),
        {"op": "FORMAT_DATETIME", "from_format": "epoch_s", "to_format": "iso8601"},
    ),
}
S3_SEGMENT_OVERRIDE = pipeline({"op": "CONSTANT", "value": "SMB"})


def mapped(
    pipelines: dict[str, Transformation],
    status: ReviewStatus = ReviewStatus.AUTO_ACCEPTED,
    **overrides: Any,
) -> tuple[MappedField, ...]:
    """Fields for ``pipelines``; ``overrides`` maps a target field to (status, pipeline|None)."""
    out = []
    for name, transformation in pipelines.items():
        out.append(_field(name, status, transformation))
    for name, (st, tr) in overrides.items():
        out = [f for f in out if f.target_field != name]
        out.append(_field(name, st, tr))
    return tuple(out)


def _field(name: str, status: ReviewStatus, transformation: Transformation | None) -> MappedField:
    return MappedField(
        target_field=name,
        mapping_type=MappingType.DIRECT if transformation else MappingType.UNRESOLVED,
        review_status=status,
        transformation=transformation,
        source_fields=transformation.source_fields if transformation else (),
    )


def s1_input(fields: tuple[MappedField, ...] | None = None) -> CodegenInput:
    return CodegenInput(
        mapping_run_id=None,
        source=model("crm.v1", "crm"),
        target=model("support.v1", "support"),
        source_entity="Customer",
        target_entity="User",
        fields=fields if fields is not None else mapped(S1_PIPELINES),
        samples=tuple(load_samples("Customer", "1", SAMPLES)),
    )


def s3_input(fields: tuple[MappedField, ...] | None = None) -> CodegenInput:
    default = mapped(
        S3_PIPELINES,
        segment=(ReviewStatus.NEEDS_REVIEW, None),
    )
    return CodegenInput(
        mapping_run_id=None,
        source=model("support.v1", "support"),
        target=model("crm.v1", "crm"),
        source_entity="User",
        target_entity="Customer",
        fields=fields if fields is not None else default,
        samples=tuple(load_samples("User", "1", SAMPLES)),
    )


def persist_run(
    session: Session,
    *,
    source: tuple[str, str],
    target: tuple[str, str],
    source_entity: str,
    target_entity: str,
    fields: tuple[MappedField, ...],
) -> int:
    """Ingest two committed specs and store a mapping run with one version per field."""
    ids = []
    for file, name in (source, target):
        spec = json.loads((ROOT / f"{file}.json").read_text(encoding="utf-8"))
        ids.append(ingest(session, parse_spec(spec, name), spec).version_id)
    run = MappingRun(
        source_version_id=ids[0], target_version_id=ids[1], source_entity=source_entity,
        target_entity=target_entity, mode="rag", provider="test", model="test",
        prompt_version="v1", confidence_version="v1", temperature=0.0, requirement=None,
        summary={}, run_reasons=[],
    )  # fmt: skip
    session.add(run)
    session.flush()
    for position, item in enumerate(fields):
        mapping = Mapping(mapping_run_id=run.id, position=position, target_field=item.target_field)
        session.add(mapping)
        session.flush()
        session.add(
            MappingVersion(
                mapping_id=mapping.id, version=1, author="system",
                mapping_type=item.mapping_type.value, source_fields=list(item.source_fields),
                transformation=item.transformation.model_dump(mode="json")
                if item.transformation else None,
                unresolved_reason=None, rationale="test", alternatives=[], certainty="HIGH",
                validation_status="PASS", validation_reasons=[], outputs_preview=[],
                confidence=0.9, review_status=item.review_status.value, review_reasons=[],
            )
        )  # fmt: skip
    session.flush()
    return run.id


def approve_all(
    session: Session, run_id: int, target_field: str, transformation: Transformation
) -> None:
    """Append a human OVERRIDDEN version, as the review API does."""
    mapping = session.scalars(
        select(Mapping).where(
            Mapping.mapping_run_id == run_id, Mapping.target_field == target_field
        )
    ).one()
    latest = session.scalars(
        select(MappingVersion)
        .where(MappingVersion.mapping_id == mapping.id)
        .order_by(MappingVersion.version.desc())
    ).first()
    assert latest is not None
    session.add(
        MappingVersion(
            mapping_id=mapping.id, version=latest.version + 1, author="human",
            mapping_type="CONSTANT", source_fields=[],
            transformation=transformation.model_dump(mode="json"), unresolved_reason=None,
            rationale="human decision", alternatives=[], certainty="HIGH",
            validation_status="PASS", validation_reasons=[], outputs_preview=[], confidence=None,
            review_status=ReviewStatus.OVERRIDDEN.value, review_reasons=["overridden"],
        )
    )  # fmt: skip
    session.flush()
