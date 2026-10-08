"""Copy a finished real repair run into ``results/repair-v0.5/<phase>/`` with its provenance.

    uv run python -m scripts.publish_repair_results --phase fixed

Writes ``results.jsonl`` (a byte copy of the run's saved results) and ``provenance.json``: the
harness commit, the generator, gate, codegen-prompt, repair-prompt and graph versions, the run date,
the model id taken from the replies themselves, the provider limits that applied, the file hashes
and an empty ``post_run_edits`` list (any later edit must be declared there). Results of a
test-only run are refused. Export the replays first (``export_repair_replays``); nothing here calls
a model.
"""

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.codegen.compiler import COMPILER_VERSION
from app.codegen.gate import GATE_VERSION
from app.codegen.generator import GENERATOR_VERSION, RUNTIME_VERSION
from app.codegen.llm_codegen import PROMPT_VERSION as CODEGEN_PROMPT_VERSION
from app.repair.graph import GRAPH_VERSION
from app.repair.prompts import REPAIR_PROMPT_VERSION

from morph_bench.repair_eval import PHASES, load_results
from morph_bench.repair_report import load_calls, sha256_lf

BENCH = Path(__file__).resolve().parents[1]
STORE_ROOT = BENCH / ".cache" / "repair-eval"
OUT_ROOT = BENCH / "results" / "repair-v0.5"
REPLAYS = BENCH / "replays" / "repair"


def versions() -> dict[str, str]:
    return {
        "generator": GENERATOR_VERSION,
        "runtime": RUNTIME_VERSION,
        "compiler": COMPILER_VERSION,
        "gate": GATE_VERSION,
        "codegen_prompt": CODEGEN_PROMPT_VERSION,
        "repair_prompt": REPAIR_PROMPT_VERSION,
        "repair_graph": GRAPH_VERSION,
    }


def build_provenance(store: Path, replays: Path, results_copy: Path) -> dict[str, Any]:
    run = json.loads((store / "provenance.json").read_text(encoding="utf-8"))
    results = load_results(results_copy)
    models = sorted({str(r["model"]) for r in load_calls(replays / "calls.jsonl").values()})
    digest = sha256_lf(results_copy)
    return {
        "schema": 1,
        "milestone": "v0.5-repair",
        "phase": run["phase"],
        "harness_sha": run["git_sha"],
        "harness_tree_dirty_at_run": run.get("git_dirty"),
        "versions": versions(),
        "run_date": str(run["started"])[:10],
        "run_started": run["started"],
        "run_finished": run.get("finished"),
        "provider": run["provider"],
        "model_configured": run["model_configured"],
        "model_reported_by_replies": "+".join(models),
        "settings": "low reasoning effort, temperature 0, at most 4000 output tokens",
        "groq_limits": {
            "tier": "Groq free tier, as seen by the key used; the tier is recorded, never changed",
            "tpm_limit_assumed": run["tpm_limit_assumed"],
            "tpm_limit_source": run["tpm_limit_source"],
            "headers_observed": run.get("limits_observed", {}),
        },
        "caps": {
            "max_real_calls": run["max_real_calls"],
            "max_real_tokens": run["max_real_tokens"],
            "budget_used": run.get("budget_used"),
        },
        "exit_status": run.get("exit_status"),
        "units": len(results),
        "files": {
            "results.jsonl": {
                "sha256": digest,
                "sha256_as_exported": digest,
                "origin": "byte copy of the run's saved results",
            },
            "hash_note": "sha256 of the file with LF line endings, as stored in git",
        },
        "post_run_edits": [],
    }


def publish(store: Path, out_dir: Path, replays: Path) -> Path:
    results_path = store / "results.jsonl"
    results = load_results(results_path)
    if not results:
        raise ValueError("no results to publish")
    if any(r.test_only for r in results):
        raise ValueError("these results come from a test-only run; they are not a real run")
    if not (replays / "expected.json").is_file():
        raise ValueError("export the replays first (scripts.export_repair_replays)")
    out_dir.mkdir(parents=True, exist_ok=True)
    copy = out_dir / "results.jsonl"
    shutil.copyfile(results_path, copy)
    provenance = build_provenance(store, replays, copy)
    path = out_dir / "provenance.json"
    path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--store-dir", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--replay-dir", type=Path)
    args = parser.parse_args(argv)
    store = args.store_dir or STORE_ROOT / args.phase
    try:
        path = publish(
            store, args.out_dir or OUT_ROOT / args.phase, args.replay_dir or REPLAYS / args.phase
        )
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
