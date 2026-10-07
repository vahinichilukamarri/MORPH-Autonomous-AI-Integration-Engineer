"""Integration bundles: content hashing, the manifest and writing a bundle to a directory.

A bundle is immutable and content-addressed. ``input_hash`` covers everything that determines the
generated code (mapping versions, spec hashes, generator and runtime versions, options);
``bundle_hash`` covers the generated files themselves.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.codegen.compiler import COMPILER_VERSION
from app.codegen.gate import GATE_VERSION
from app.codegen.generator import GENERATOR_VERSION, RUNTIME_VERSION
from app.codegen.inputs import CodegenInput
from app.codegen.review_gate import GateDecision

MANIFEST_SCHEMA = 1


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def bundle_hash(files: Mapping[str, str]) -> str:
    return sha256_text(canonical(sorted((path, sha256_text(text)) for path, text in files.items())))


def input_hash(
    inp: CodegenInput, condition: str, *, allow_partial: bool, llm_identity: str = ""
) -> str:
    fields = [
        {
            "target_field": m.target_field,
            "mapping_type": m.mapping_type.value,
            "review_status": m.review_status.value,
            "transformation": m.transformation.model_dump(mode="json")
            if m.transformation
            else None,
        }
        for m in sorted(inp.fields, key=lambda f: f.target_field)
    ]
    return sha256_text(
        canonical(
            {
                "generator": GENERATOR_VERSION,
                "runtime": RUNTIME_VERSION,
                "compiler": COMPILER_VERSION,
                "gate": GATE_VERSION,
                "condition": condition,
                "allow_partial": allow_partial,
                "llm": llm_identity,
                "source": [inp.source.spec_hash, inp.source_entity],
                "target": [inp.target.spec_hash, inp.target_entity],
                "fields": fields,
                "samples": sha256_text(canonical(list(inp.samples))),
            }
        )
    )


def build_manifest(
    inp: CodegenInput,
    *,
    condition: str,
    status: str,
    decision: GateDecision | None,
    strategy: dict[str, Any] | None,
    files: Mapping[str, str],
    plan_error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "compiler_version": COMPILER_VERSION,
        "gate_version": GATE_VERSION,
        "condition": condition,
        "status": status,
        "mapping_run_id": inp.mapping_run_id,
        "source": {
            "system": inp.source.name,
            "api_version": inp.source.api_version,
            "spec_hash": inp.source.spec_hash,
            "entity": inp.source_entity,
        },
        "target": {
            "system": inp.target.name,
            "api_version": inp.target.api_version,
            "spec_hash": inp.target.spec_hash,
            "entity": inp.target_entity,
        },
        "mapping_versions": [
            {
                "target_field": m.target_field,
                "mapping_version_id": m.mapping_version_id,
                "version": m.mapping_version,
                "author": m.author,
                "review_status": m.review_status.value,
            }
            for m in inp.fields
        ],
        "plan_error": plan_error,
        "review": (
            None
            if decision is None
            else {
                "gate_status": decision.status.value,
                "included": [m.target_field for m in decision.included],
                "excluded": [e.as_dict() for e in decision.excluded],
                "not_writable": list(decision.not_writable),
                "blocking": [e.as_dict() for e in decision.blocking],
            }
        ),
        "strategy": strategy,
        "files": [{"path": p, "sha256": sha256_text(t)} for p, t in sorted(files.items())],
        "bundle_hash": bundle_hash(files) if files else None,
    }


def write_bundle(directory: Path, files: Mapping[str, str], manifest: Mapping[str, Any]) -> Path:
    """Write the package and manifest under ``directory`` (created if needed)."""
    for path, text in files.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
    )
    return directory
