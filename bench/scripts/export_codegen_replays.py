"""Turn the saved real L1/L2 run into committed replay fixtures. Run from ``bench/``:

    uv run python -m scripts.export_codegen_replays

Writes ``replays/codegen/calls.jsonl`` (every real model reply, keyed by the plain prompt hash,
no key and no headers) and ``replays/codegen/expected.json`` (what each unit produced, so a
replay test can compare). Nothing is invented: both files are copies of saved results.
"""

import json
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
STORE = BENCH / ".cache" / "codegen-eval" / "real"
OUT = BENCH / "replays" / "codegen"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    lines: list[str] = []
    for line in (STORE / "calls" / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        record["prompt_hash"] = record["prompt_hash"].rsplit(":", 1)[0]  # drop the run index
        if record["prompt_hash"] not in seen:
            seen.add(record["prompt_hash"])
            lines.append(json.dumps(record, sort_keys=True))
    (OUT / "calls.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    expected = []
    for line in (STORE / "results.jsonl").read_text(encoding="utf-8").splitlines():
        unit = json.loads(line)
        if unit["condition"] == "D":
            continue
        llm = unit.get("llm") or {}
        expected.append(
            {
                "scenario_id": unit["scenario_id"],
                "input_set": unit["input_set"],
                "condition": unit["condition"],
                "status": unit["status"],
                "gate": unit["gate"],
                "gate_findings": unit["gate_findings"],
                "blocked_reasons": unit["blocked_reasons"],
                "calls": llm.get("calls", 0),
                "input_tokens": llm.get("input_tokens", 0),
                "output_tokens": llm.get("output_tokens", 0),
                "reasoning_tokens": llm.get("reasoning_tokens", 0),
            }
        )
    (OUT / "expected.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"{len(lines)} replies, {len(expected)} units")


if __name__ == "__main__":
    main()
