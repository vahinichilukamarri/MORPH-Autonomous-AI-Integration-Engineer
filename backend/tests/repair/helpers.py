"""Bundles for the repair tests, built the way the generator builds them (no database, no model)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.codegen.bundle import write_bundle
from app.codegen.generated_tests import render_tests
from app.codegen.generator import derive_strategy, generate_package
from app.codegen.inputs import CodegenInput
from app.codegen.operations import OperationPlan, analyse
from app.codegen.review_gate import GateDecision, decide
from app.mapping.confidence import ReviewStatus
from tests.codegen.fixtures import (
    S3_PIPELINES,
    S3_SEGMENT_OVERRIDE,
    mapped,
    s1_input,
    s3_input,
)

CONTROLS = Path(__file__).resolve().parent / "controls"


def control(name: str) -> str:
    return (CONTROLS / f"{name}.py.txt").read_text(encoding="utf-8")


def s3_approved() -> CodegenInput:
    return s3_input(mapped(S3_PIPELINES, segment=(ReviewStatus.OVERRIDDEN, S3_SEGMENT_OVERRIDE)))


@dataclass(frozen=True)
class Built:
    inp: CodegenInput
    plan: OperationPlan
    decision: GateDecision
    files: dict[str, str]
    strategy: dict[str, Any]  # what D derives; the shape the smoke test needs

    @property
    def base_tests(self) -> dict[str, str]:
        return render_tests(self.decision.included, self.inp.samples)

    def write(self, directory: Path) -> Path:
        return write_bundle(directory / "bundle", self.files, {})


def build(
    inp: CodegenInput,
    *,
    sync_source: str | None = None,
    extra_samples: tuple[dict[str, Any], ...] = (),
) -> Built:
    """D's bundle, or an L2 bundle when ``sync_source`` is given."""
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=False)
    package = generate_package(inp, plan, decision, sync_source=sync_source)
    files = {
        **package.files,
        **render_tests(decision.included, (*inp.samples, *extra_samples)),
    }
    return Built(inp, plan, decision, files, derive_strategy(plan, decision.included))


def s1_built(sync_source: str | None = None) -> Built:
    return build(s1_input(), sync_source=sync_source)
