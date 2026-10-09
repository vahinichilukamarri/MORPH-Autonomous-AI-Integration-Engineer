"""The 13 tool handlers: thin adapters over the existing services.

A handler runs only after the gateway has validated the arguments, applied the floor and the
policy, and (where needed) used up an approval. It receives a ``Runtime`` that holds a database
session, the policy context the decision was made on, and a meter. Nothing here decides anything
about policy; everything that reads stored text wraps it as untrusted; everything that calls a
model or the sandbox does so through the metered wrappers, so a repair run's calls are charged one
by one.

Handlers never read or write ``system_policy_attributes`` and never touch approvals.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.codegen.inputs import InputError, load_input
from app.codegen.sandbox import SandboxError, SandboxRunner
from app.codegen.service import GenerationError, generate, run_generated_tests
from app.db_models import (
    AuditEvent,
    EntityRow,
    Integration,
    IntegrationVersion,
    Mapping,
    MappingRun,
    RepairRun,
    System,
)
from app.discovery.errors import ParseError
from app.discovery.parser import parse_spec
from app.discovery.repository import ingest
from app.discovery.repository import latest_version as latest_system_version
from app.discovery.source import load_spec
from app.embeddings.provider import EmbeddingProvider
from app.embeddings.service import embed_version
from app.llm.base import BaseLLMProvider, LLMError
from app.llm.store import ResponseStore
from app.mapping.runner import MappingRunError, run_mapping
from app.mapping.store import latest_version as latest_mapping_version
from app.mapping.store import save_run
from app.mcp_server import envelope
from app.mcp_server.metering import Meter, MeteredProvider, MeteredRunner
from app.policy import attributes
from app.policy.attributes import Attributes
from app.policy.audit import AuditLog
from app.policy.floor import withhold_test_data
from app.policy.loader import LoadedPolicy
from app.policy.models import CallContext, DataClass, Role
from app.policy.toolspec import (
    Args,
    GenerateIntegrationArgs,
    GetIntegrationArgs,
    GetIntegrationFilesArgs,
    GetMappingRunArgs,
    GetRepairRunArgs,
    GetSystemArgs,
    IngestContractArgs,
    ListAuditEventsArgs,
    ListSystemsArgs,
    ProposeMappingArgs,
    RepairIntegrationArgs,
    RunGeneratedTestsArgs,
)
from app.repair.prompts import RepairPromptBuilder
from app.repair.service import RepairEnv, resume_repair, run_repair
from app.repair.state import StartMode
from app.settings import Settings

Body = dict[str, Any]


class ToolError(Exception):
    """A tool could not do what was asked for a reason the caller can act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Deps:
    """Everything a handler may use, built once by the server (or by a test)."""

    settings: Settings
    policy: LoadedPolicy
    audit: AuditLog
    role: Role
    session_id: str
    spec_root: Any
    samples_dir: Any
    response_store_dir: Any
    conninfo: str
    secret_values: tuple[str, ...]
    provider_factory: Callable[[], BaseLLMProvider]
    embedder_factory: Callable[[], EmbeddingProvider]
    runner_factory: Callable[[], SandboxRunner]
    smoke_runner_factory: Callable[[], SandboxRunner]


@dataclass
class Runtime:
    db: Session
    deps: Deps
    ctx: CallContext
    meter: Meter
    call_id: str
    notes: Body = field(default_factory=dict)  # facts to report even if the call stops early

    def provider(self) -> BaseLLMProvider:
        try:
            return MeteredProvider(self.deps.provider_factory(), self.meter)
        except LLMError as error:
            raise ToolError("MODEL_UNAVAILABLE", str(error)) from error

    def runner(self) -> SandboxRunner:
        return MeteredRunner(self.deps.runner_factory(), self.meter)

    def smoke_runner(self) -> SandboxRunner:
        return MeteredRunner(self.deps.smoke_runner_factory(), self.meter)

    @property
    def synthetic(self) -> bool:
        return self.ctx.data_class is DataClass.SYNTHETIC


Handler = Callable[[Runtime, Any], Body]


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def not_found(what: str, ident: int) -> ToolError:
    return ToolError("NOT_FOUND", f"{what} {ident} does not exist")


# ---- read-only tools -----------------------------------------------------------------------------


def list_systems(rt: Runtime, args: ListSystemsArgs) -> Body:
    rows = rt.db.scalars(select(System).order_by(System.name).limit(args.limit)).all()
    out = []
    for system in rows:
        latest = latest_system_version(rt.db, system.id)
        found: Attributes = attributes.get_attributes(rt.db, system.id)
        out.append(
            {
                "id": system.id,
                "name": system.name,
                "latest_version": latest.version if latest else None,
                "latest_version_id": latest.id if latest else None,
                "environment": found.environment.value,
                "data_class": found.data_class.value,
                "attributes_recorded": found.recorded,
            }
        )
    return {"systems": out, "notice": envelope.NOTICE}


def get_system(rt: Runtime, args: GetSystemArgs) -> Body:
    system = rt.db.get(System, args.system_id)
    if system is None:
        raise not_found("system", args.system_id)
    latest = latest_system_version(rt.db, system.id)
    found = attributes.get_attributes(rt.db, system.id)
    body: Body = {
        "id": system.id,
        "name": system.name,
        "environment": found.environment.value,
        "data_class": found.data_class.value,
        "attributes_recorded": found.recorded,
        "versions": [
            {
                "id": v.id,
                "version": v.version,
                "spec_hash": v.spec_hash,
                "ingested_at": iso(v.ingested_at),
            }
            for v in system.versions
        ],
        "notice": envelope.NOTICE,
    }
    if latest is not None:
        entities = rt.db.scalars(
            select(EntityRow)
            .where(EntityRow.system_version_id == latest.id)
            .order_by(EntityRow.position)
        ).all()
        body["latest_version_id"] = latest.id
        body["api_title"] = envelope.untrusted("api_title", latest.api_title)
        body["entities"] = [
            {
                "name": e.name,
                "role": e.role,
                "description": envelope.untrusted("entity_description", e.description),
                "fields": [
                    {
                        "name": f.name,
                        "path": f.path,
                        "type": f.json_type,
                        "format": f.format,
                        "nullable": f.nullable,
                        "required": f.required,
                        "enum_values": envelope.untrusted_list("enum_value", list(f.enum_values)),
                        "description": envelope.untrusted("field_description", f.description),
                    }
                    for f in e.fields
                ],
            }
            for e in entities
        ]
    return body


def get_mapping_run(rt: Runtime, args: GetMappingRunArgs) -> Body:
    run = rt.db.get(MappingRun, args.mapping_run_id)
    if run is None:
        raise not_found("mapping run", args.mapping_run_id)
    mappings = rt.db.scalars(
        select(Mapping).where(Mapping.mapping_run_id == run.id).order_by(Mapping.position)
    ).all()
    fields = []
    for mapping in mappings:
        version = latest_mapping_version(rt.db, mapping.id)
        fields.append(
            {
                "target_field": mapping.target_field,
                "version": version.version,
                "author": version.author,
                "mapping_type": version.mapping_type,
                "source_fields": list(version.source_fields),
                "review_status": version.review_status,
                "review_reasons": list(version.review_reasons),
                "validation_status": version.validation_status,
                "confidence": version.confidence,
                "rationale": envelope.untrusted("mapping_rationale", version.rationale),
                "unresolved_reason": envelope.untrusted(
                    "unresolved_reason", version.unresolved_reason
                ),
            }
        )
    return {
        "id": run.id,
        "source_version_id": run.source_version_id,
        "target_version_id": run.target_version_id,
        "source_entity": run.source_entity,
        "target_entity": run.target_entity,
        "mode": run.mode,
        "provider": run.provider,
        "model": run.model,
        "summary": run.summary,
        "mappings": fields,
        "notice": envelope.NOTICE,
    }


def _version_view(rt: Runtime, version: IntegrationVersion) -> Body:
    gates = [
        {
            "stage": g.stage,
            "passed": g.passed,
            "findings": [
                {
                    "rule": f.get("rule"),
                    "file": f.get("file"),
                    "line": f.get("line"),
                    "message": envelope.untrusted("gate_message", str(f.get("message", ""))),
                }
                for f in g.findings
            ],
        }
        for g in version.gate_results
    ]
    runs = []
    for row in version.sandbox_runs:
        view: Body = {
            "id": row.id,
            "purpose": row.purpose,
            "outcome": row.outcome,
            "exit_code": row.exit_code,
            "duration_s": row.duration_s,
        }
        result = row.result or {}
        view["tests"] = {"total": result.get("total"), "passed": result.get("passed")}
        if rt.synthetic:
            view["failures"] = result.get("failures", [])
            view["stdout_excerpt"] = envelope.untrusted("sandbox_stdout", row.stdout_excerpt)
        else:
            view["withheld"] = "failure details and output are returned only for synthetic data"
        runs.append(view)
    return {
        "version": version.version,
        "status": version.status,
        "bundle_hash": version.bundle_hash,
        "input_hash": version.input_hash,
        "generator_version": version.generator_version,
        "review": version.manifest.get("review"),
        "gate_results": gates,
        "sandbox_runs": runs,
    }


def _integration_or_error(rt: Runtime, integration_id: int) -> Integration:
    integration = rt.db.get(Integration, integration_id)
    if integration is None:
        raise not_found("integration", integration_id)
    return integration


def _version_or_error(integration: Integration, number: int) -> IntegrationVersion:
    for version in integration.versions:
        if version.version == number:
            return version
    raise ToolError("NOT_FOUND", f"integration {integration.id} has no version {number}")


def get_integration(rt: Runtime, args: GetIntegrationArgs) -> Body:
    integration = _integration_or_error(rt, args.integration_id)
    body: Body = {
        "id": integration.id,
        "mapping_run_id": integration.mapping_run_id,
        "condition": integration.condition,
        "versions": [v.version for v in integration.versions],
        "notice": envelope.NOTICE,
    }
    if args.version is not None:
        body["detail"] = _version_view(rt, _version_or_error(integration, args.version))
    elif integration.versions:
        body["detail"] = _version_view(rt, integration.versions[-1])
    return body


def get_integration_files(rt: Runtime, args: GetIntegrationFilesArgs) -> Body:
    integration = _integration_or_error(rt, args.integration_id)
    version = _version_or_error(integration, args.version)
    listing = [
        {"path": f.path, "kind": f.kind, "sha256": f.sha256, "content": f.content}
        for f in version.files
    ]
    kept, withheld = withhold_test_data(listing, rt.ctx.data_class)
    for item in kept:
        item["content"] = envelope.untrusted("generated_file", str(item["content"]))
    return {"integration_id": integration.id, "version": version.version, "files": kept,
            "withheld": withheld, "notice": envelope.NOTICE}  # fmt: skip


def get_repair_run(rt: Runtime, args: GetRepairRunArgs) -> Body:
    run = rt.db.get(RepairRun, args.run_id)
    if run is None:
        raise not_found("repair run", args.run_id)
    attempts = []
    for a in run.attempts:
        feedback = [
            {
                "stage": i.get("stage"),
                "code": i.get("code"),
                "message": envelope.untrusted("feedback_message", str(i.get("message", ""))),
            }
            for i in (a.feedback or {}).get("items", [])
        ]
        view: Body = {
            "attempt": a.attempt,
            "source": a.source,
            "failed_stage": a.failed_stage,
            "output_hash": a.output_hash,
            "output_chars": len(a.output_text),
            "guards": [g.get("guard") for g in a.guard_result or []],
            "feedback": feedback,
            "total_tokens": a.total_tokens,
            "integration_version_id": a.integration_version_id,
        }
        if rt.synthetic:
            view["output"] = envelope.untrusted("model_output", a.output_text)
        else:
            view["output_withheld"] = "the model output is returned only for synthetic data"
        attempts.append(view)
    return {
        "id": run.id,
        "mapping_run_id": run.mapping_run_id,
        "condition": run.condition,
        "start_mode": run.start_mode,
        "status": run.status,
        "terminal_reason": envelope.untrusted("terminal_reason", run.terminal_reason),
        "pauses": run.pauses,
        "attempts": attempts,
        "notice": envelope.NOTICE,
    }


def list_audit_events(rt: Runtime, args: ListAuditEventsArgs) -> Body:
    query = select(AuditEvent).where(
        AuditEvent.chain == rt.deps.audit.chain,
        AuditEvent.session_id == rt.deps.session_id,
        AuditEvent.seq > (args.after_seq or 0),
    )
    rows = rt.db.scalars(query.order_by(AuditEvent.seq).limit(args.limit)).all()
    return {
        "session_id": rt.deps.session_id,
        "events": [
            {"seq": r.seq, "type": r.event_type, "tool": r.tool, "call_id": r.call_id,
             "payload": r.payload}
            for r in rows
        ],
    }  # fmt: skip


def describe_policy(rt: Runtime, args: Args) -> Body:
    policy = rt.deps.policy
    return {
        "version": policy.version,
        "hash": policy.hash,
        "your_role": rt.deps.role.value,
        "description": policy.file.description,
        "session_limits": policy.file.session_limits.model_dump(),
        "rules": [
            {"id": r.id, "decision": r.decision.value, "description": r.description}
            for r in policy.file.rules
        ],
    }


# ---- tools with side effects ---------------------------------------------------------------------


def ingest_contract(rt: Runtime, args: IngestContractArgs) -> Body:
    path = (rt.deps.spec_root / args.file).resolve()  # the floor already checked it stays inside
    try:
        spec = load_spec(str(path))
        model = parse_spec(spec, args.name)
    except ParseError as error:
        problems = [
            {"pointer": p.pointer, "message": envelope.untrusted("spec_problem", p.message)}
            for p in error.problems
        ]
        raise ToolError(
            "INVALID_SPEC", f"the contract is not a valid OpenAPI document: {problems}"
        ) from error
    result = ingest(rt.db, model, spec)
    stats = embed_version(rt.db, result.version_id, rt.deps.embedder_factory())
    rt.db.commit()
    return {
        "system_id": result.system_id,
        "name": args.name,
        "version": result.version,
        "version_id": result.version_id,
        "created": result.created,
        "entities": len(model.entities),
        "fields": sum(len(e.fields) for e in model.entities),
        "fields_embedded": stats.fields_embedded,
        "next_step": "A human must record this system's environment and data class (approver "
        "token, PUT /systems/{id}/policy-attributes) before code runs or the model is used on it.",
    }


def propose_mapping(rt: Runtime, args: ProposeMappingArgs) -> Body:
    settings = rt.deps.settings
    try:
        result = run_mapping(
            rt.db, rt.provider(), rt.deps.embedder_factory(),
            source_version_id=args.source_system_version,
            target_version_id=args.target_system_version,
            source_entity=args.source_entity, target_entity=args.target_entity, mode="rag",
            samples_dir=rt.deps.samples_dir, requirement=None,
            temperature=settings.llm_temperature, max_output_tokens=settings.llm_max_output_tokens,
        )  # fmt: skip
    except MappingRunError as error:
        raise ToolError("NOT_FOUND", str(error)) from error
    run = save_run(rt.db, result, settings.llm_temperature)
    rt.db.commit()
    return {"mapping_run_id": run.id, "summary": run.summary, "model": run.model}


def generate_integration(rt: Runtime, args: GenerateIntegrationArgs) -> Body:
    settings = rt.deps.settings
    llm = rt.provider() if args.condition != "D" else None
    try:
        version = generate(
            rt.db, args.mapping_run_id, condition=args.condition, runner=rt.runner(),
            samples_dir=rt.deps.samples_dir, llm=llm, temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
        )  # fmt: skip
    except InputError as error:
        raise ToolError("NOT_FOUND", str(error)) from error
    except GenerationError as error:
        raise ToolError("GENERATION_FAILED", str(error)) from error
    rt.db.commit()
    review = version.manifest.get("review") or {}
    return {
        "integration_id": version.integration_id,
        "version": version.version,
        "status": version.status,
        "bundle_hash": version.bundle_hash,
        "gate": {g.stage: g.passed for g in version.gate_results},
        "review_gate": review.get("gate_status"),
        "blocking": [
            {
                "field": b.get("target_field"),
                "reason": envelope.untrusted("block_reason", str(b.get("reason"))),
            }
            for b in review.get("blocking", [])
        ],
    }


def run_generated_tests_tool(rt: Runtime, args: RunGeneratedTestsArgs) -> Body:
    integration = _integration_or_error(rt, args.integration_id)
    version = _version_or_error(integration, args.version)
    try:
        row = run_generated_tests(rt.db, version, rt.runner())
    except GenerationError as error:
        raise ToolError("NOT_RUNNABLE", str(error)) from error
    except SandboxError as error:
        raise ToolError("SANDBOX_UNAVAILABLE", str(error)) from error
    rt.db.commit()
    result = row.result or {}
    body: Body = {
        "sandbox_run_id": row.id,
        "outcome": row.outcome,
        "exit_code": row.exit_code,
        "tests": {"total": result.get("total"), "passed": result.get("passed")},
    }
    if rt.synthetic:
        body["failures"] = result.get("failures", [])
    return body


def repair_integration(rt: Runtime, args: RepairIntegrationArgs) -> Body:
    settings = rt.deps.settings
    if args.run_id is not None:
        existing = rt.db.get(RepairRun, args.run_id)
        if existing is None:
            raise not_found("repair run", args.run_id)
        mapping_run_id, condition = existing.mapping_run_id, existing.condition
    else:
        assert args.mapping_run_id is not None and args.condition is not None
        mapping_run_id, condition = args.mapping_run_id, args.condition
    try:
        inp = load_input(rt.db, mapping_run_id, rt.deps.samples_dir)
    except InputError as error:
        raise ToolError("NOT_FOUND", str(error)) from error
    env = RepairEnv(
        session=rt.db, inp=inp, condition=condition, start_mode=StartMode.FRESH,
        builder=RepairPromptBuilder(
            temperature=settings.llm_temperature, max_output_tokens=settings.llm_max_output_tokens
        ),
        live=rt.provider(), response_store=ResponseStore(rt.deps.response_store_dir),
        runner=rt.runner(), smoke_runner=rt.smoke_runner(), conninfo=rt.deps.conninfo,
        secrets=rt.deps.secret_values,
        on_created=lambda run_id: rt.notes.update({"repair_run_id": run_id}),
    )  # fmt: skip
    result = resume_repair(env, args.run_id) if args.run_id is not None else run_repair(env)
    return {
        "repair_run_id": result.run_id,
        "status": result.status,
        "reason": envelope.untrusted("terminal_reason", result.reason),
        "model_turns": result.attempts,
        "pauses": result.pauses,
        "next_step": "Read the attempts with get_repair_run. READY means the gate, the generated "
        "tests and the smoke test passed; it does not mean the integration is correct.",
    }


HANDLERS: dict[str, Handler] = {
    "list_systems": list_systems,
    "get_system": get_system,
    "get_mapping_run": get_mapping_run,
    "get_integration": get_integration,
    "get_integration_files": get_integration_files,
    "get_repair_run": get_repair_run,
    "list_audit_events": list_audit_events,
    "describe_policy": describe_policy,
    "ingest_contract": ingest_contract,
    "propose_mapping": propose_mapping,
    "generate_integration": generate_integration,
    "run_generated_tests": run_generated_tests_tool,
    "repair_integration": repair_integration,
}
