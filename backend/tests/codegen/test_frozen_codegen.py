"""Codegen prompts v1 and generator v1 are frozen before the first real run.

If a test here fails, do not edit the expected value: introduce prompts/v2 or generator v2 and
report the new version as a separate, labelled run (see docs/codegen-eval.md). Retuning on the
same scenarios and reporting only the better result is not allowed.
"""

import hashlib

from app.codegen.bundle import bundle_hash
from app.codegen.compiler import COMPILER_VERSION
from app.codegen.gate import GATE_VERSION
from app.codegen.generated_tests import render_tests
from app.codegen.generator import GENERATOR_VERSION, RUNTIME_VERSION, generate_package
from app.codegen.llm_codegen import PROMPT_DIR, PROMPT_VERSION, build_l1_request, build_l2_request
from app.codegen.operations import analyse
from app.codegen.review_gate import decide
from app.mapping.confidence import ReviewStatus
from tests.codegen.fixtures import S3_PIPELINES, S3_SEGMENT_OVERRIDE, mapped, s1_input, s3_input

FROZEN_PROMPT_HASHES = {
    "l1_user.md": "495c1ffa1b859ed01f1ef79ae321533d7c83940381f5fdef7c4a7041bb6f863d",
    "l2_user.md": "438471c11073d80ab6b93572eb622629173a8802e9a2c75d0b731d6a2297e976",
    "runtime_api.md": "ddfa429c692be3cc74f937ef47864d844dff62bfb6939ee8538288fd5f4fb62d",
    "system.md": "9193ea0763fd022eb217056d62eac5e784dd72e1885bcba333f9d071a7a350b5",
}
# system text + user text of the rendered S1 requests (the golden prompts)
GOLDEN_RENDERED = {
    "l1": "39c3d35cf5960ae3650d13ddf4d6f5adcf529948313fd58b16934dcf20282163",
    "l2": "b4b18e2edd762f128b327deb95886ca055ed38889c04686567d64ba69a0b8dd2",
}
FROZEN_BUNDLE_HASHES = {
    "s1": "e85a55ee13380b614c1c4e9f5cddc349d890831fbbd7b222372bd9661d61a51e",
    "s3": "750754bf139a0c1d594ca831788235d64f3ed66c0fee273c00cfd404a5dc2e5e",
}


def test_codegen_prompt_templates_v1_are_unchanged() -> None:
    assert PROMPT_VERSION == "codegen-v1"
    for name, expected in FROZEN_PROMPT_HASHES.items():
        data = (PROMPT_DIR / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == expected, f"{name} changed"
    assert {p.name for p in PROMPT_DIR.iterdir()} == set(FROZEN_PROMPT_HASHES)


def test_rendered_codegen_prompts_for_s1_are_unchanged() -> None:
    inp = s1_input()
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=False)
    rendered = {
        "l1": build_l1_request(inp, decision),
        "l2": build_l2_request(inp, decision),
    }
    for name, request in rendered.items():
        digest = hashlib.sha256(
            (request.system + "\n---\n" + request.parts[0]).encode()
        ).hexdigest()
        assert digest == GOLDEN_RENDERED[name], f"the rendered {name} prompt changed"


def test_generator_v1_versions_and_condition_d_bundles_are_unchanged() -> None:
    assert (GENERATOR_VERSION, RUNTIME_VERSION, COMPILER_VERSION, GATE_VERSION) == (
        "1",
        "1",
        "1",
        "1",
    )
    s3 = s3_input(mapped(S3_PIPELINES, segment=(ReviewStatus.OVERRIDDEN, S3_SEGMENT_OVERRIDE)))
    for name, inp in (("s1", s1_input()), ("s3", s3)):
        plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
        decision = decide(inp, plan, allow_partial=False)
        files = {
            **generate_package(inp, plan, decision).files,
            **render_tests(decision.included, inp.samples),
        }
        assert bundle_hash(files) == FROZEN_BUNDLE_HASHES[name], f"the {name} bundle changed"
