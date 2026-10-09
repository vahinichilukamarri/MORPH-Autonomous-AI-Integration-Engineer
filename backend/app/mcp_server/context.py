"""Which systems a call involves, found from the database, never from the caller's words.

The policy looks at the environment and data class of the *systems* a call touches. The caller only
supplies ids; this module follows them (integration, mapping run, repair run, system versions) to
the systems. An id that does not exist involves no system, and the tool then answers "not found".
"""

from sqlalchemy.orm import Session

from app.db_models import Integration, MappingRun, RepairRun, SystemVersion
from app.policy.toolspec import (
    Args,
    GenerateIntegrationArgs,
    GetIntegrationArgs,
    GetIntegrationFilesArgs,
    GetMappingRunArgs,
    GetRepairRunArgs,
    GetSystemArgs,
    ProposeMappingArgs,
    RepairIntegrationArgs,
    RunGeneratedTestsArgs,
)


def _versions_to_systems(db: Session, version_ids: list[int]) -> list[int]:
    systems: list[int] = []
    for version_id in version_ids:
        version = db.get(SystemVersion, version_id)
        if version is not None and version.system_id not in systems:
            systems.append(version.system_id)
    return systems


def _mapping_run_systems(db: Session, mapping_run_id: int | None) -> list[int]:
    run = db.get(MappingRun, mapping_run_id) if mapping_run_id is not None else None
    if run is None:
        return []
    return _versions_to_systems(db, [run.source_version_id, run.target_version_id])


def _integration_systems(db: Session, integration_id: int) -> list[int]:
    integration = db.get(Integration, integration_id)
    return _mapping_run_systems(db, integration.mapping_run_id if integration else None)


def _repair_systems(db: Session, run_id: int) -> list[int]:
    run = db.get(RepairRun, run_id)
    return _mapping_run_systems(db, run.mapping_run_id if run else None)


def involved_systems(db: Session, args: Args) -> list[int]:
    """System ids a call touches (empty if it touches none, or if an id does not exist)."""
    if isinstance(args, ProposeMappingArgs):
        return _versions_to_systems(db, [args.source_system_version, args.target_system_version])
    if isinstance(args, GenerateIntegrationArgs | GetMappingRunArgs):
        return _mapping_run_systems(db, args.mapping_run_id)
    if isinstance(args, GetIntegrationArgs | GetIntegrationFilesArgs | RunGeneratedTestsArgs):
        return _integration_systems(db, args.integration_id)
    if isinstance(args, RepairIntegrationArgs):
        if args.run_id is not None:
            return _repair_systems(db, args.run_id)
        return _mapping_run_systems(db, args.mapping_run_id)
    if isinstance(args, GetRepairRunArgs):
        return _repair_systems(db, args.run_id)
    if isinstance(args, GetSystemArgs):
        return [args.system_id]
    return []
