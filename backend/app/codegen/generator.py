"""Condition D: render a complete integration package deterministically (no LLM).

The package is ``integration/{__init__,__main__,clients,strategy,transform}.py``:

* ``transform.py``: compiled from the approved DSL pipelines (``compiler``);
* ``clients.py``: base URLs and credentials from the environment, auth scheme from discovery;
* ``strategy.py``: the sync strategy derived from the discovered operations;
* ``__main__.py``: a fixed template that hands all of that to the trusted ``morph_runtime``.
"""

from dataclasses import dataclass
from typing import Any

from app.codegen.compiler import COMPILER_VERSION, FieldSpec, compile_transform_module
from app.codegen.inputs import CodegenInput, MappedField
from app.codegen.operations import AuthPlan, OperationPlan, PlanCode, PlanError
from app.codegen.review_gate import GateDecision
from app.mapping.transform import Constant

GENERATOR_VERSION = "1"
RUNTIME_VERSION = "1"

MAIN = '''"""Entry point: python -m integration. Generated; do not edit."""

from morph_runtime.cli import main

from integration import clients, strategy, transform

raise SystemExit(
    main(strategy.STRATEGY, clients.source_client, clients.target_client, transform.to_target)
)
'''


@dataclass(frozen=True)
class GeneratedPackage:
    files: dict[str, str]
    strategy: dict[str, Any]


def _auth_expr(auth: AuthPlan, credential_var: str) -> str:
    credential = f'env("{credential_var}")'
    if auth.kind == "apiKey":
        return f"ApiKeyAuth({auth.header!r}, {credential})"
    return f"BearerAuth({credential})"


def _client_function(role: str, auth: AuthPlan) -> str:
    url_var = f"MORPH_{role.upper()}_URL"
    credential_var = f"MORPH_{role.upper()}_CREDENTIAL"
    return (
        f"def {role}_client() -> HttpClient:\n"
        "    return make_client(\n"
        f'        "{url_var}",\n'
        f"        {_auth_expr(auth, credential_var)},\n"
        "    )\n"
    )


def render_clients(plan: OperationPlan) -> str:
    kinds = {plan.source.auth.kind, plan.target.auth.kind}
    imports = ", ".join(
        name for name, kind in (("ApiKeyAuth", "apiKey"), ("BearerAuth", "bearer")) if kind in kinds
    )
    header = (
        '"""Clients for the two systems. Generated; do not edit."""\n\n'
        f"from morph_runtime.auth import {imports}\n"
        "from morph_runtime.config import env, make_client\n"
        "from morph_runtime.http import HttpClient\n\n\n"
    )
    source = _client_function("source", plan.source.auth)
    target = _client_function("target", plan.target.auth)
    return header + source + "\n\n" + target


def derive_strategy(
    plan: OperationPlan, included: tuple[MappedField, ...], page_size: int = 100
) -> dict[str, Any]:
    """The strategy as plain data (also stored in the manifest)."""
    s, t = plan.source, plan.target
    names = {m.target_field for m in included}
    constants = {
        m.target_field
        for m in included
        if m.transformation is not None and isinstance(m.transformation.steps[0], Constant)
    }
    create_fields = tuple(f.name for f in t.create_fields if f.name in names)
    update_fields = tuple(f.name for f in t.update_fields if f.name in names)
    if t.mode == "UPSERT":
        if t.id_field not in names:
            raise PlanError(PlanCode.NO_TARGET_LOOKUP, "the target identity field is not mapped")
        create_only: tuple[str, ...] = ()
    else:
        create_only = tuple(f for f in create_fields if f in constants)
        update_fields = tuple(f for f in update_fields if f not in create_only)
        if t.natural_key is not None and t.natural_key not in names:
            raise PlanError(PlanCode.NO_NATURAL_KEY, f"{t.natural_key} is not part of the mapping")
    omit_if_null = tuple(
        f.name for f in t.create_fields if f.name in names and not f.required and not f.nullable
    )
    return {
        "source": {
            "mode": s.mode,
            "key_field": s.key_field,
            "list_path": s.list_path,
            "page_param": s.page_param,
            "size_param": s.size_param,
            "page_size": page_size,
            "items_key": s.items_key,
            "total_key": s.total_key,
            "get_path": s.get_path,
        },
        "target": {
            "mode": t.mode,
            "id_field": t.id_field,
            "get_path": t.get_path,
            "update_method": t.update_method,
            "update_path": t.update_path,
            "create_path": t.create_path,
            "create_fields": create_fields,
            "update_fields": update_fields,
            "create_only": create_only,
            "omit_if_null": omit_if_null,
            "natural_key": t.natural_key,
            "id_assigned_by_target": t.id_assigned_by_target,
            "list_path": t.list_path,
            "page_param": t.page_param,
            "size_param": t.size_param,
            "page_size": page_size,
            "items_key": t.items_key,
            "total_key": t.total_key,
        },
    }


def _call(name: str, values: dict[str, Any], enums: dict[str, str]) -> str:
    args = []
    for key, value in values.items():
        rendered = f"{enums[key]}.{value}" if key in enums else repr(value)
        args.append(f"        {key}={rendered},\n")
    return f"{name}(\n" + "".join(args) + "    )"


def render_strategy(strategy: dict[str, Any]) -> str:
    source = _call("SourceSpec", strategy["source"], {"mode": "SourceMode"})
    target = _call("TargetSpec", strategy["target"], {"mode": "TargetMode"})
    return (
        '"""Sync strategy derived from the discovered operations. Generated; do not edit."""\n\n'
        "from morph_runtime.sync import (\n"
        "    SourceMode,\n    SourceSpec,\n    Strategy,\n    TargetMode,\n    TargetSpec,\n"
        ")\n\n"
        f"STRATEGY = Strategy(\n    source={source},\n    target={target},\n)\n"
    )


def generate_package(
    inp: CodegenInput, plan: OperationPlan, decision: GateDecision
) -> GeneratedPackage:
    included = decision.included
    strategy = derive_strategy(plan, included)
    specs = [FieldSpec(m.target_field, m.transformation) for m in included if m.transformation]
    files = {
        "integration/__init__.py": "",
        "integration/__main__.py": MAIN,
        "integration/clients.py": render_clients(plan),
        "integration/strategy.py": render_strategy(strategy),
        "integration/transform.py": compile_transform_module(specs),
    }
    return GeneratedPackage(files=files, strategy=strategy)


__all__ = ["COMPILER_VERSION", "GENERATOR_VERSION", "RUNTIME_VERSION", "generate_package"]
