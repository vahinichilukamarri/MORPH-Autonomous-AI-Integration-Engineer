"""A gateway on the throwaway database with every outside dependency replaced by a double.

The model is a scripted double (no network), the sandbox is the stub the repair tests already use,
the embedder is the fake one, and the secrets are sentinels. Systems are created with unique names
so tests that commit for real do not see each other's data.
"""

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.codegen.sandbox import SandboxRunner
from app.db_models import MappingRun, SystemVersion
from app.embeddings.fake import FakeEmbeddingProvider
from app.llm.base import BaseLLMProvider, CallMetadata, LLMRequest, RawCompletion
from app.mcp_server.adapters import Deps
from app.mcp_server.gateway import Gateway, ToolResponse
from app.policy import attributes
from app.policy.audit import AuditLog
from app.policy.clock import ManualClock
from app.policy.loader import LoadedPolicy, load_active, policy_hash
from app.policy.models import DataClass, Environment, Role, SessionLimits
from app.repair.service import ensure_checkpoint_schema
from app.settings import Settings
from tests.codegen.fixtures import ROOT as OPENAPI_DIR
from tests.codegen.fixtures import S1_PIPELINES, SAMPLES, mapped, persist_run
from tests.repair.support import StubRunner, conninfo

SECRETS = {
    "model_key": "gsk_SENTINELkey0123456789abcdef",
    "approver": "approver-sentinel-token-7f3a91",
    "password": "s3ntinelPassw0rd",
    "database_url": "postgresql+psycopg://morph:s3ntinelPassw0rd@localhost:5432/morph",
}


class ModelDouble(BaseLLMProvider):
    """Replies in order; when the script runs out it answers with an invalid object."""

    name = "double"

    def __init__(self, replies: list[str] | None = None, default: str = "{}") -> None:
        self.replies = list(replies or [])
        self.default = default
        self.calls = 0

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        self.calls += 1
        text = self.replies.pop(0) if self.replies else self.default
        meta = CallMetadata(
            "double", "double-model", request.fingerprint(response_model), 1, 100, 10, 0,
            total_tokens=110, usage={"total_tokens": 110}, finish_reason="stop", source="scripted",
        )  # fmt: skip
        return RawCompletion(text, meta)


def with_limits(policy: LoadedPolicy, **limits: int) -> LoadedPolicy:
    """The same policy with different session limits (a test double; the hash is recomputed)."""
    current = policy.file.session_limits.model_dump()
    current.update(limits)
    changed = policy.file.model_copy(update={"session_limits": SessionLimits(**current)})
    return LoadedPolicy(changed, policy_hash(changed), policy.path)


@dataclass
class World:
    mapping_run_id: int
    source_system_id: int
    target_system_id: int
    source_version_id: int
    target_version_id: int


@dataclass
class Rig:
    gateway: Gateway
    deps: Deps
    engine: Engine
    audit: AuditLog
    clock: ManualClock
    model: ModelDouble
    runner: SandboxRunner
    spec_root: Path
    tag: str
    role: Role
    extra: dict[str, Any] = field(default_factory=dict)

    def call(self, tool: str, **arguments: Any) -> ToolResponse:
        return self.gateway.call(tool, arguments)

    def world(
        self,
        environment: Environment | None = Environment.MOCK,
        data_class: DataClass | None = DataClass.SYNTHETIC,
        *,
        label: str = "",
    ) -> World:
        """Two ingested systems and an approved mapping run; attributes recorded unless None."""
        tag = f"{self.tag}{label}{uuid.uuid4().hex[:4]}"
        with Session(self.engine) as db:
            run_id = persist_run(
                db, source=("crm.v1", f"crm-{tag}"), target=("support.v1", f"support-{tag}"),
                source_entity="Customer", target_entity="User", fields=mapped(S1_PIPELINES),
            )  # fmt: skip
            db.commit()
            run = db.get(MappingRun, run_id)
            assert run is not None
            versions = [
                db.get(SystemVersion, v) for v in (run.source_version_id, run.target_version_id)
            ]
            assert all(v is not None for v in versions)
            system_ids = [v.system_id for v in versions if v is not None]
            world = World(run_id, system_ids[0], system_ids[1], run.source_version_id,
                          run.target_version_id)  # fmt: skip
            if environment is not None and data_class is not None:
                for sid in system_ids:
                    attributes.set_attributes(
                        db, self.audit, self.clock, sid, environment=environment,
                        data_class=data_class, updated_by="human:approver", session_id="test",
                    )  # fmt: skip
        return world


def make_rig(
    engine: Engine,
    tmp_path: Path,
    *,
    role: Role = Role.OPERATOR,
    policy: LoadedPolicy | None = None,
    replies: list[str] | None = None,
    session_id: str | None = None,
    samples_dir: Path | None = None,
    runner: SandboxRunner | None = None,
) -> Rig:
    tag = uuid.uuid4().hex[:6]
    spec_root = tmp_path / "specs"
    spec_root.mkdir(parents=True, exist_ok=True)
    for name in ("crm.v1.json", "support.v1.json"):
        (spec_root / name).write_bytes((OPENAPI_DIR / name).read_bytes())
    clock = ManualClock()
    audit = AuditLog(engine, chain=f"gw-{tag}", clock=clock)
    model = ModelDouble(replies)
    stub = runner or StubRunner()
    info = conninfo(engine)
    ensure_checkpoint_schema(info)
    settings = Settings.model_validate({})
    deps = Deps(
        settings=settings, policy=policy or load_active(), audit=audit, role=role,
        session_id=session_id or f"s-{tag}", spec_root=spec_root,
        samples_dir=samples_dir or SAMPLES, response_store_dir=tmp_path / "responses",
        conninfo=info, secret_values=tuple(SECRETS.values()),
        provider_factory=lambda: model, embedder_factory=FakeEmbeddingProvider,
        runner_factory=lambda: stub, smoke_runner_factory=lambda: stub,
    )  # fmt: skip
    gateway = Gateway(deps=deps, engine=engine, clock=clock)
    return Rig(gateway, deps, engine, audit, clock, model, stub, spec_root, tag, role)
