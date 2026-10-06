from pathlib import Path
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db_models import Base, EntityRow, FieldRow, OperationRow, System, SystemVersion
from app.discovery.parser import parse_spec
from app.discovery.repository import (
    get_version,
    ingest,
    latest_version,
    load_model,
)
from app.discovery.source import load_spec

OPENAPI_DIR = Path(__file__).resolve().parents[3] / "mock_systems" / "openapi"


def spec(name: str) -> dict[str, Any]:
    return load_spec(OPENAPI_DIR / f"{name}.json")


def ingest_file(session: Session, system: str, file: str):  # type: ignore[no-untyped-def]
    raw = spec(file)
    return ingest(session, parse_spec(raw, system), raw)


def count(session: Session, table: type[Base]) -> int:
    return session.scalar(select(func.count()).select_from(table)) or 0


def test_migration_matches_the_orm_models(test_engine: Engine) -> None:
    with test_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)
    assert diff == []


def test_first_ingest_creates_version_one(session: Session) -> None:
    result = ingest_file(session, "crm", "crm.v1")
    assert (result.version, result.created) == (1, True)
    version = session.get(SystemVersion, result.version_id)
    assert version is not None
    assert version.spec_json == spec("crm.v1")
    assert version.api_title == "Mock CRM"
    assert count(session, System) == 1


def test_ingest_is_idempotent(session: Session) -> None:
    first = ingest_file(session, "crm", "crm.v1")
    rows = (count(session, EntityRow), count(session, FieldRow), count(session, OperationRow))
    again = ingest_file(session, "crm", "crm.v1")
    assert again.created is False
    assert (again.version, again.version_id) == (first.version, first.version_id)
    assert (
        count(session, EntityRow),
        count(session, FieldRow),
        count(session, OperationRow),
    ) == rows
    assert count(session, SystemVersion) == 1


def test_changed_spec_creates_version_two_and_keeps_version_one(session: Session) -> None:
    v1 = ingest_file(session, "crm", "crm.v1")
    before = load_model(session, v1.version_id)
    v2 = ingest_file(session, "crm", "crm.v2")
    assert (v2.version, v2.created) == (2, True)
    assert v2.system_id == v1.system_id
    assert v2.version_id != v1.version_id

    # both versions are stored and independently queryable
    assert get_version(session, v1.system_id, 1) is not None
    assert get_version(session, v1.system_id, 2) is not None
    latest = latest_version(session, v1.system_id)
    assert latest is not None and latest.version == 2

    # version 1 is exactly as it was: nothing was overwritten
    after = load_model(session, v1.version_id)
    assert after == before
    assert after == parse_spec(spec("crm.v1"), "crm")

    v1_fields = {f.name for e in after.entities if e.name == "Customer" for f in e.fields}
    v2_model = load_model(session, v2.version_id)
    v2_fields = {f.name for e in v2_model.entities if e.name == "Customer" for f in e.fields}
    assert "phone" in v1_fields and "phone_number" not in v1_fields
    assert "phone_number" in v2_fields and "phone" not in v2_fields


def test_rows_belong_to_their_own_version(session: Session) -> None:
    v1 = ingest_file(session, "crm", "crm.v1")
    v2 = ingest_file(session, "crm", "crm.v2")
    for result in (v1, v2):
        entities = session.scalars(
            select(EntityRow).where(EntityRow.system_version_id == result.version_id)
        ).all()
        assert {e.name for e in entities} >= {"Customer", "CustomerPage"}
    assert count(session, EntityRow) == 2 * 6


def test_stored_model_round_trips_for_every_spec(session: Session) -> None:
    for system, file in (("crm", "crm.v1"), ("support", "support.v1"), ("support", "support.v2")):
        raw = spec(file)
        expected = parse_spec(raw, system)
        result = ingest(session, expected, raw)
        assert load_model(session, result.version_id) == expected


def test_systems_are_independent(session: Session) -> None:
    crm = ingest_file(session, "crm", "crm.v1")
    support = ingest_file(session, "support", "support.v1")
    assert crm.system_id != support.system_id
    assert (crm.version, support.version) == (1, 1)
