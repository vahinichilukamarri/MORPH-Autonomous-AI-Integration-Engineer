"""Turn a saved real repair run into committed replay fixtures. Run from ``bench/``:

    uv run python -m scripts.export_repair_replays --phase fixed

Writes ``replays/repair/<phase>/calls.jsonl`` (every real model reply of the phase, keyed by the
plain prompt hash, which already contains the attempt number and the feedback; no key, no header, no
prompt text) and ``replays/repair/<phase>/expected.json`` (what each unit did, attempt by attempt,
so a replay test can compare). Nothing is invented: both files are copies of saved results. A
fixed-start unit's attempt 0 is not in ``calls.jsonl``: it comes from the committed v0.4 replays.
"""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from app.llm.store import ResponseStore

from morph_bench.repair_eval import PHASES, RepairUnitResult, load_results

BENCH = Path(__file__).resolve().parents[1]
STORE_ROOT = BENCH / ".cache" / "repair-eval"
OUT_ROOT = BENCH / "replays" / "repair"


def expected_unit(unit: RepairUnitResult) -> dict[str, object]:
    return {
        "scenario_id": unit.scenario_id,
        "condition": unit.condition,
        "start_mode": unit.start_mode,
        "status": unit.status,
        "reason": unit.reason,
        "model_turns": unit.model_turns,
        "attempts": [
            {
                "attempt": a.attempt,
                "failed_stage": a.failed_stage,
                "output_hash": a.output_hash,
                "feedback_codes": a.feedback_codes,
                "guard_enforced": a.guard_enforced,
                "guard_shadow": a.guard_shadow,
            }
            for a in unit.attempts
        ],
    }


def export(store_dir: Path, out_dir: Path, *, allow_test_only: bool = False) -> tuple[int, int]:
    """Copy the real replies and the units' outcomes; returns (replies, units).

    Results of a test-only run (scripted or replayed, not a model) are refused: committed replay
    fixtures are copies of real runs.
    """
    results = load_results(store_dir / "results.jsonl")
    if any(u.test_only for u in results) and not allow_test_only:
        raise ValueError("these results come from a test-only run; they are not a real run")
    responses = ResponseStore(store_dir / "responses")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for path in sorted((store_dir / "responses").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                lines.append(json.loads(line))
    seen: set[str] = set()
    unique = []
    for record in sorted(lines, key=lambda r: r["prompt_hash"]):
        if record["prompt_hash"] not in seen:
            seen.add(record["prompt_hash"])
            unique.append(json.dumps(record, sort_keys=True, ensure_ascii=False))
    (out_dir / "calls.jsonl").write_text("\n".join(unique) + ("\n" if unique else ""), "utf-8")
    units = sorted(results, key=lambda u: (u.scenario_id, u.condition))
    expected = [expected_unit(u) for u in units]
    (out_dir / "expected.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    assert len(responses) == len(unique), "every stored reply was exported"
    return len(unique), len(expected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--store-dir", type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args(argv)
    store = args.store_dir or STORE_ROOT / args.phase
    out = args.out_dir or OUT_ROOT / args.phase
    if not (store / "results.jsonl").is_file():
        print(f"nothing to export: no {store / 'results.jsonl'}")
        return 1
    replies, units = export(store, out)
    print(f"{replies} replies, {units} units -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
