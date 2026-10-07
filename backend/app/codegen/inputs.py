"""The only inputs code generation may use: discovery models and mapping versions.

Everything here is built from the database (or, in tests, from parsed specs). Nothing reads
answer keys, reference pipelines or oracle fixtures.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import Mapping, MappingRun, MappingVersion, SystemVersion
from app.discovery.models import SystemModel
from app.discovery.repository import load_model
from app.mapping.confidence import ReviewStatus
from app.mapping.proposal import MappingType
from app.mapping.samples import load_samples
from app.mapping.transform import JsonScalar, Transformation

APPROVED_STATUSES = frozenset(
    {ReviewStatus.AUTO_ACCEPTED, ReviewStatus.APPROVED, ReviewStatus.OVERRIDDEN}
)


class InputError(Exception):
    pass


@dataclass(frozen=True)
class MappedField:
    target_field: str
    mapping_type: MappingType
    review_status: ReviewStatus
    transformation: Transformation | None
    source_fields: tuple[str, ...]
    mapping_version_id: int | None = None
    mapping_version: int | None = None
    author: str = "system"

    @property
    def approved(self) -> bool:
        return (
            self.review_status in APPROVED_STATUSES
            and self.mapping_type is not MappingType.UNRESOLVED
            and self.transformation is not None
        )


@dataclass(frozen=True)
class CodegenInput:
    mapping_run_id: int | None
    source: SystemModel
    target: SystemModel
    source_entity: str
    target_entity: str
    fields: tuple[MappedField, ...]
    source_version_id: int | None = None
    target_version_id: int | None = None
    samples: tuple[dict[str, JsonScalar], ...] = ()


def load_input(
    session: Session, mapping_run_id: int, samples_dir: Path | None = None
) -> CodegenInput:
    run = session.get(MappingRun, mapping_run_id)
    if run is None:
        raise InputError(f"mapping run {mapping_run_id} does not exist")
    fields: list[MappedField] = []
    for mapping in session.scalars(
        select(Mapping).where(Mapping.mapping_run_id == run.id).order_by(Mapping.position)
    ):
        latest = session.scalars(
            select(MappingVersion)
            .where(MappingVersion.mapping_id == mapping.id)
            .order_by(MappingVersion.version.desc())
            .limit(1)
        ).one()
        fields.append(
            MappedField(
                target_field=mapping.target_field,
                mapping_type=MappingType(latest.mapping_type),
                review_status=ReviewStatus(latest.review_status),
                transformation=(
                    Transformation.model_validate(latest.transformation)
                    if latest.transformation
                    else None
                ),
                source_fields=tuple(latest.source_fields),
                mapping_version_id=latest.id,
                mapping_version=latest.version,
                author=latest.author,
            )
        )
    for version_id in (run.source_version_id, run.target_version_id):
        if session.get(SystemVersion, version_id) is None:
            raise InputError(f"system version {version_id} does not exist")
    source_model = load_model(session, run.source_version_id)
    samples = (
        tuple(load_samples(run.source_entity, source_model.api_version, samples_dir))
        if samples_dir is not None
        else ()
    )
    return CodegenInput(
        mapping_run_id=run.id,
        source=source_model,
        target=load_model(session, run.target_version_id),
        source_entity=run.source_entity,
        target_entity=run.target_entity,
        fields=tuple(fields),
        source_version_id=run.source_version_id,
        target_version_id=run.target_version_id,
        samples=samples,
    )
