"""The mock systems' admin API must be invisible to discovery, mapping prompts and codegen.

Proof by construction: everything the pipeline can show to a model or write into a bundle is built
from the public OpenAPI documents, the sample fixtures and the mapping versions. These tests
check each of those, plus the backend source itself.
"""

import json
import re
from pathlib import Path

import pytest

from app.codegen.generator import generate_package
from app.codegen.operations import analyse
from app.codegen.review_gate import decide
from app.discovery.parser import parse_spec
from app.mapping import prompts
from app.mapping.confidence import ReviewStatus
from app.mapping.samples import load_samples
from app.mapping.transform import JsonScalar
from tests.codegen.fixtures import (
    S3_PIPELINES,
    S3_SEGMENT_OVERRIDE,
    SAMPLES,
    mapped,
    s1_input,
    s3_input,
)

REPO = Path(__file__).resolve().parents[3]
ADMIN = re.compile(r"admin", re.IGNORECASE)
# the frozen prompt says "an administrator" in its injection warning; that is not the mock API
ADMIN_API = re.compile(r"__admin|x-admin|admin[_-]?token|/admin", re.IGNORECASE)
SPECS = sorted((REPO / "mock_systems" / "openapi").glob("*.json"))


def test_committed_specs_and_samples_never_mention_admin() -> None:
    files = [*SPECS, *sorted((REPO / "mock_systems" / "samples").glob("*.json"))]
    assert len(files) >= 8
    offenders = [f.name for f in files if ADMIN.search(f.read_text(encoding="utf-8"))]
    assert offenders == []


@pytest.mark.parametrize("spec", SPECS, ids=[s.stem for s in SPECS])
def test_discovery_model_has_no_admin_operation_entity_or_field(spec: Path) -> None:
    document = json.loads(spec.read_text(encoding="utf-8"))
    model = parse_spec(document, spec.stem.split(".")[0])
    assert not ADMIN.search(model.model_dump_json())
    assert all("__" not in op.path for op in model.operations)


@pytest.mark.parametrize(
    ("spec", "entity", "samples_entity"),
    [
        ("crm.v1", "Customer", "Customer"),
        ("support.v1", "User", "User"),
    ],
)
def test_no_mapping_prompt_mentions_admin(spec: str, entity: str, samples_entity: str) -> None:
    from tests.codegen.fixtures import model

    system = model(spec, spec.split(".")[0])
    other = model("support.v1" if spec.startswith("crm") else "crm.v1", "x")
    source = system.entity(entity)
    target = other.entity("User" if spec.startswith("crm") else "Customer")
    assert target is not None
    assert source is not None
    samples: list[dict[str, JsonScalar]] = load_samples(samples_entity, "1", SAMPLES)
    texts = [prompts.system_prompt()]
    for target_field in target.fields:
        for mode in ("full_schema", "rag"):
            retrieved = [
                prompts.RetrievedField(rank, f, 0.1 * rank)
                for rank, f in enumerate(source.fields, 1)
            ]
            texts.append(
                prompts.build_user_prompt(
                    mode,
                    target_field,
                    source.fields,
                    retrieved,
                    samples,
                    "Keep the systems in sync.",
                )
            )
    assert len(texts) > 10
    assert [t[:40] for t in texts if ADMIN_API.search(t)] == []


def test_generated_bundles_never_mention_admin_or_internal_paths() -> None:
    bundles = []
    s1 = s1_input()
    plan = analyse(s1.source, s1.source_entity, s1.target, s1.target_entity)
    bundles.append(generate_package(s1, plan, decide(s1, plan, allow_partial=False)))
    s3 = s3_input(mapped(S3_PIPELINES, segment=(ReviewStatus.OVERRIDDEN, S3_SEGMENT_OVERRIDE)))
    plan = analyse(s3.source, s3.source_entity, s3.target, s3.target_entity)
    bundles.append(generate_package(s3, plan, decide(s3, plan, allow_partial=False)))
    for package in bundles:
        text = "\n".join(package.files.values())
        assert not ADMIN.search(text)
        assert "/__" not in text


def test_the_backend_source_never_names_the_admin_path() -> None:
    app_dir = REPO / "backend" / "app"
    offenders = [
        p.relative_to(app_dir).as_posix()
        for p in app_dir.rglob("*.py")
        if "__admin" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.docker
def test_live_systems_serve_no_admin_path_and_it_still_works_for_trusted_callers() -> None:
    import urllib.request

    from app.codegen.sandbox import MockEnvironment

    with MockEnvironment() as mocks:
        for system, port in (("crm", mocks.crm_host_port), ("support", mocks.support_host_port)):
            url = f"http://127.0.0.1:{port}/openapi.json"
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
                served = response.read().decode("utf-8")
            assert not ADMIN.search(served), system
            model = parse_spec(json.loads(served), system)
            assert not ADMIN.search(model.model_dump_json()), system
        request = urllib.request.Request(  # noqa: S310
            mocks.crm_admin_url + "/__admin/state",
            headers={"X-Admin-Token": mocks.crm_admin_token},
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            assert response.status == 200  # reachable for trusted callers, absent from the spec
