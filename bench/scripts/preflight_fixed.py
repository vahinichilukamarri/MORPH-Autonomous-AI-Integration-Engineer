"""Pre-flight for the fixed-start run: the whole pipeline against fake and replay providers.

    uv run python -m scripts.preflight_fixed

Makes **no real model call** and never builds a real provider. It runs the real gate, tests and
smoke test in the sandbox, with a scripted model in place of Groq, and prints the evidence the
fixed-start run's go depends on: blocked units make zero calls, the size estimate of every repair
request against the hard stop, the attempt-0 seeds against the recorded v0.4 replies, the
fail-closed
usage check, the caps, and a scan of everything printed for secrets. Exit code 0 only if every check
passed.
"""

import io
import json
import os
import re
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from app.codegen.inputs import load_input
from app.codegen.llm_codegen import StrategyProposal, SyncModuleProposal
from app.codegen.operations import analyse
from app.codegen.review_gate import decide
from app.codegen.sandbox import SandboxRunner
from app.llm.base import BaseLLMProvider, CallMetadata, LLMRequest, RawCompletion
from app.llm.store import ResponseStore
from app.repair.feedback import FeedbackItem, Stage, build_feedback
from app.repair.prompts import RepairPromptBuilder
from app.repair.service import RepairEnv, ensure_checkpoint_schema, run_repair
from app.repair.sizing import HARD_STOP_FACTOR, estimate_request
from app.repair.smoke import make_smoke_runner
from app.repair.state import StartMode
from app.settings import get_settings
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from morph_bench.loader import SCENARIOS_ROOT, discover
from morph_bench.mapping_eval import Limits
from morph_bench.oracle.build import SAMPLES_DIR, seed_mapping_run
from morph_bench.oracle.models import load_fixture
from morph_bench.repair_eval import (
    PHASE_CALL_CAPS,
    SHORT,
    BudgetedProvider,
    BudgetExhausted,
    MissingUsageError,
    RepairUnitResult,
    check_recorded_calls,
    load_results,
)
from scripts.run_codegen_eval import SHORT_IDS, proposed_run
from scripts.run_mapping_eval import ResumableProvider
from scripts.run_repair_eval import (
    CONDITIONS,
    EXIT_OK,
    V04_REPLAYS,
    check_options,
    main,
    verify_recording_path,
)

STORE_ROOT = Path(__file__).resolve().parents[1] / ".cache" / "repair-eval"
DAILY_TOKEN_CAP = 200_000  # Groq's published daily limit for the model; see the pre-registration
TOKEN_CAP = int(DAILY_TOKEN_CAP * 0.8)
SECRET_PATTERNS = (r"gsk_[A-Za-z0-9]{10,}", r"(?i)bearer\s+\S{8,}", r"sk-[A-Za-z0-9]{20,}")
FIRST_CALL = {  # index of each unit's first recorded v0.4 reply (the seed for attempt 0)
    ("S1", "L1R"): 0, ("S1", "L2R"): 2, ("S4", "L1R"): 3, ("S4", "L2R"): 5,
    ("S3", "L1R"): 6, ("S3", "L2R"): 8,
}  # fmt: skip

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class Tripwire(BaseLLMProvider):
    """Any call is a failure of the check that holds it."""

    name = "tripwire"

    def __init__(self) -> None:
        self.calls = 0

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        self.calls += 1
        raise AssertionError("a model was called")


class FakeModel(BaseLLMProvider):
    """A scripted stand-in for Groq: every reply is invalid JSON, so a unit uses all 3 repairs."""

    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def complete_raw(self, request: LLMRequest, response_model: type[BaseModel]) -> RawCompletion:
        self.calls += 1
        meta = CallMetadata(
            "fake", "fake-model", request.fingerprint(response_model), 1, 100, 10, 0,
            total_tokens=110, usage={"total_tokens": 110}, finish_reason="stop", source="network",
        )  # fmt: skip
        return RawCompletion(f"not json {self.calls}", meta)


def model_for(condition: str) -> type[BaseModel]:
    return StrategyProposal if condition == "L1R" else SyncModuleProposal


def unit_inputs(session: Session, scenario_id: str):  # type: ignore[no-untyped-def]
    run_id = seed_mapping_run(session, load_fixture(scenario_id), with_override=True)
    inp = load_input(session, run_id, SAMPLES_DIR)
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    return inp, decide(inp, plan, allow_partial=False)


def section_blocked(session: Session, conninfo: str, tmp: Path) -> None:
    print("\n1. Blocked units make zero calls (the v0.3 as-proposed mappings, fixed start)")
    settings = get_settings()
    bundles = {
        SHORT_IDS[b.scenario.id]: b for b in discover(SCENARIOS_ROOT) if b.scenario.id in SHORT_IDS
    }
    builder = RepairPromptBuilder(
        temperature=settings.llm_temperature, max_output_tokens=settings.llm_max_output_tokens
    )
    for short in ("S1", "S3", "S4"):
        run_id = proposed_run(session, bundles[short], None)
        inp = load_input(session, run_id, SAMPLES_DIR)
        for condition in CONDITIONS:
            live, seed = Tripwire(), Tripwire()
            env = RepairEnv(
                session=session, inp=inp, condition=condition, start_mode=StartMode.FIXED,
                builder=builder, live=live, response_store=ResponseStore(tmp / f"b-{short}"),
                runner=SandboxRunner(), smoke_runner=make_smoke_runner(), conninfo=conninfo,
                seed=seed,
            )  # fmt: skip
            result = run_repair(env)
            check(
                f"{short} {condition} as-proposed",
                result.status.startswith("BLOCKED") and live.calls == 0 and seed.calls == 0,
                f"{result.status}; live calls {live.calls}, seed reads {seed.calls}",
            )


def section_pipeline(engine_url: str, tmp: Path) -> list[RepairUnitResult]:
    print("\n2. The full fixed-start pipeline (real gate, tests and smoke test; scripted model)")
    store = tmp / "pipeline"
    model = FakeModel()
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        code = main(
            ["--phase", "fixed", "--test-only", "--store-dir", str(store), "--database-url",
             engine_url],
            llm_override=model,
            oracle_override=lambda bundle, scenario: None,
        )  # fmt: skip
    check("the harness ran to the end", code == EXIT_OK, f"exit code {code}")
    results = load_results(store / "results.jsonl")
    check("six units finished", len(results) == 6, f"{len(results)} units")
    check(
        "the scripted model was called only for repairs",
        model.calls == sum(u.real_calls for u in results) == 18,
        f"{model.calls} calls (3 per unit)",
    )
    return results


def section_seeds(results: list[RepairUnitResult]) -> None:
    print("\n3. Seeds load from the recorded v0.4 replies")
    recorded = [
        json.loads(line) for line in (V04_REPLAYS / "calls.jsonl").read_text("utf-8").splitlines()
    ]
    for unit in sorted(results, key=lambda u: (SHORT[u.scenario_id], u.condition)):
        short = SHORT[unit.scenario_id]
        attempt0 = unit.attempts[0]
        want = recorded[FIRST_CALL[(short, unit.condition)]]
        check(
            f"{short} {unit.condition} attempt 0",
            attempt0.source == "replay" and attempt0.prompt_hash == want["prompt_hash"],
            f"served by replay; prompt hash {str(attempt0.prompt_hash)[:12]} is the recorded one",
        )


def section_sizes(session: Session, results: list[RepairUnitResult]) -> list[int]:
    print("\n4. Size of each repair request (ESTIMATE) against the hard stop")
    settings = get_settings()
    limit = int(8000 * HARD_STOP_FACTOR)
    builder = RepairPromptBuilder(
        temperature=settings.llm_temperature, max_output_tokens=settings.llm_max_output_tokens
    )
    long_feedback = build_feedback(
        0,
        [
            FeedbackItem(Stage.AST, f"RULE{n}", "m" * 150, "integration/sync.py", n)
            for n in range(12)
        ],
        history=[(0, ["A.B"])],
    )
    recorded = [
        json.loads(line) for line in (V04_REPLAYS / "calls.jsonl").read_text("utf-8").splitlines()
    ]
    print(
        f"  hard stop: more than {limit} tokens (8000 x {HARD_STOP_FACTOR}); "
        "the unit would be 'not run'"
    )
    print("  | unit | repair 1, real feedback | upper bound, 12 long feedback items | stopped? |")
    bounds: list[int] = []
    for unit in sorted(results, key=lambda u: (SHORT[u.scenario_id], u.condition)):
        short = SHORT[unit.scenario_id]
        inp, decision = unit_inputs(session, unit.scenario_id)
        seed = recorded[FIRST_CALL[(short, unit.condition)]]["text"]
        request = builder.repair(
            inp, decision, unit.condition, attempt=1, previous_output=seed, feedback=long_feedback
        )
        bound = estimate_request(request, model_for(unit.condition), unit.condition).total_tokens
        real = unit.attempts[1].size_estimate_tokens if len(unit.attempts) > 1 else None
        bounds.append(bound)
        print(
            f"  | {short} {unit.condition} | {real} | {bound} | "
            f"{'YES' if bound > limit else 'no'} |"
        )
    check("no estimate is above the hard stop", all(b <= limit for b in bounds))
    total = sum(b * 3 for b in bounds)
    print(
        f"  worst case 18 calls at the upper bound: about {total} tokens (ESTIMATE, input plus "
        "expected "
        f"output) against the cap of {TOKEN_CAP}"
    )
    return bounds


def section_usage(tmp: Path) -> None:
    print("\n5. Fail-closed usage")
    try:
        verify_recording_path()
        check("the recording path keeps usage, total_tokens and finish_reason", True)
    except MissingUsageError as error:
        check("the recording path keeps usage", False, str(error))
    check("no stored real call is missing usage", check_recorded_calls(STORE_ROOT / "fixed") == [])

    class NoUsage(FakeModel):
        def complete_raw(
            self, request: LLMRequest, response_model: type[BaseModel]
        ) -> RawCompletion:
            raw = super().complete_raw(request, response_model)
            bare = replace(raw.metadata, usage=None, total_tokens=None)
            return RawCompletion(raw.text, bare)

    provider = ResumableProvider(NoUsage(), tmp / "lossy", limits=Limits(), require_usage=True)
    request = LLMRequest("s", ("p",), "schema")
    try:
        provider.complete_raw(request, StrategyProposal)
        check("negative control: a reply without usage stops the run", False)
    except MissingUsageError:
        check("negative control: a reply without usage stops the run", True)


def section_caps(tmp: Path) -> None:
    print("\n6. Caps")
    base = dict(
        phase="fixed", provider="groq", test_only=False, confirm=True, max_calls=20,
        max_tokens=TOKEN_CAP, conditions=["L1R", "L2R"],
    )  # fmt: skip
    cases: list[tuple[str, dict[str, object], bool]] = [
        ("the planned real run is accepted", {}, False),
        ("21 real calls are refused", {"max_calls": 21}, True),
        ("a real run without --confirm-real-run is refused", {"confirm": False}, True),
        ("a real run without a token cap is refused", {"max_tokens": None}, True),
        ("the replay provider outside --test-only is refused", {"provider": "replay"}, True),
    ]
    for label, override, refused in cases:
        reason = check_options(**{**base, **override})  # type: ignore[arg-type]
        check(label, (reason is not None) == refused, reason or "accepted")
    check("the fixed-phase call cap is 20", PHASE_CALL_CAPS["fixed"] == 20)
    check("the token cap is 0.8 x the daily limit", TOKEN_CAP == 160_000, str(TOKEN_CAP))

    class Quiet(FakeModel):
        pass

    request = LLMRequest("s", ("p",), "schema")
    capped = BudgetedProvider(Quiet(), tmp / "calls-cap.json", max_calls=2, max_tokens=None)
    capped.complete_raw(request, StrategyProposal)
    capped.complete_raw(request, StrategyProposal)
    try:
        capped.complete_raw(request, StrategyProposal)
        check("a third call past a cap of 2 is refused", False)
    except BudgetExhausted as stop:
        check("a third call past a cap of 2 is refused", stop.which == "max-real-calls", str(stop))
    tokens = BudgetedProvider(Quiet(), tmp / "tokens-cap.json", max_calls=20, max_tokens=500)
    try:
        tokens.complete_raw(request, StrategyProposal)
        check("a request that would pass the token cap is refused", False)
    except BudgetExhausted as stop:
        check("a request that would pass the token cap is refused", stop.which == "max-real-tokens")
    check("the control calls did not count as real calls anywhere else", capped.state.calls == 2)


def section_environment() -> None:
    print("\n7. Environment")
    settings = get_settings()
    tracing = [v for v in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2") if os.environ.get(v)]
    check("no tracing variable is set", not tracing)
    check(
        "settings equal the v0.4 settings",
        (settings.llm_temperature, settings.llm_max_output_tokens, settings.groq_reasoning_effort)
        == (0.0, 4000, "low"),
        f"temperature {settings.llm_temperature}, max output {settings.llm_max_output_tokens}, "
        f"reasoning effort {settings.groq_reasoning_effort}",
    )
    print(f"  model configured: {settings.groq_model}")
    key_set = settings.groq_api_key is not None and bool(settings.groq_api_key.get_secret_value())
    check("a Groq key is configured (value never printed)", key_set)
    fresh = not (STORE_ROOT / "fixed").exists()
    check("no earlier fixed-phase store exists (budget and results start at zero)", fresh)


def scan_for_secrets(text: str) -> None:
    print("\n8. Secret scan of everything printed above")
    settings = get_settings()
    values = []
    if settings.groq_api_key is not None:
        values.append(settings.groq_api_key.get_secret_value())
    hits = [p for p in SECRET_PATTERNS if re.search(p, text)]
    hits += ["the configured key value" for v in values if v and v in text]
    check("no key value or token-like string in the output", not hits, ", ".join(hits))


class Tee(io.TextIOBase):
    def __init__(self, stream: io.TextIOBase) -> None:
        self.stream = stream
        self.buffer = io.StringIO()

    def write(self, text: str) -> int:
        self.buffer.write(text)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()


def run() -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    url = engine.url.render_as_string(hide_password=False)
    ensure_checkpoint_schema(conninfo)
    with tempfile.TemporaryDirectory(prefix="morph-preflight-") as name, Session(engine) as session:
        tmp = Path(name)
        section_blocked(session, conninfo, tmp)
        results = section_pipeline(url, tmp)
        section_seeds(results)
        section_sizes(session, results)
        section_usage(tmp)
        section_caps(tmp)
        section_environment()
    return 0


def main_preflight() -> int:
    original = sys.stdout
    tee = Tee(original)  # type: ignore[arg-type]
    sys.stdout = tee
    try:
        run()
        scan_for_secrets(tee.buffer.getvalue())
    finally:
        sys.stdout = original
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + '; '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main_preflight())
