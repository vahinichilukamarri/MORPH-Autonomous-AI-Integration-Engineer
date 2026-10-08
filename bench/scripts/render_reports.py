"""Regenerate the committed evaluation reports from committed files. Run from ``bench/``:

    uv run python -m scripts.render_reports            # rewrite the reports
    uv run python -m scripts.render_reports --check    # rewrite, then fail on any difference

``docs/codegen-eval.md`` comes from ``results/codegen-v0.4``; ``docs/repair-eval.md`` (and
``docs/repair-eval-fresh.md``) from ``results/repair-v0.5/<phase>`` plus the committed replays under
``replays/repair/<phase>``. Before rendering, each results directory's provenance is verified (its
files must still hash to the recorded values: an undeclared edit fails) and the repair results are
cross-checked against the replays. With ``--check`` the rewritten reports must then equal what is in
git (``git diff --exit-code``), so a hand-edited report fails too. No model, no database, no Docker.
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from morph_bench.codegen_eval import load_results as load_codegen
from morph_bench.codegen_eval import render_report as render_codegen
from morph_bench.repair_eval import load_results as load_repair
from morph_bench.repair_report import (
    cross_check,
    load_calls,
    render_repair_report,
    verify_provenance,
)
from morph_bench.systems import REPO_ROOT

BENCH = Path(__file__).resolve().parents[1]
CODEGEN_DIR = BENCH / "results" / "codegen-v0.4"
REPAIR_ROOT = BENCH / "results" / "repair-v0.5"
REPLAYS = BENCH / "replays"
DOCS = REPO_ROOT / "docs"
REPAIR_DOCS = {"fixed": DOCS / "repair-eval.md", "fresh": DOCS / "repair-eval-fresh.md"}


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def render_all(
    *, codegen_dir: Path = CODEGEN_DIR, repair_root: Path = REPAIR_ROOT,
    replays: Path = REPLAYS, docs: dict[str, Path] | None = None, codegen_doc: Path | None = None,
) -> tuple[list[Path], list[str]]:  # fmt: skip
    """Write every report that has committed results; returns the files written and the problems."""
    written: list[Path] = []
    problems: list[str] = []
    if (codegen_dir / "results.jsonl").is_file():
        problems += verify_provenance(codegen_dir, repair=False)
        provenance = json.loads((codegen_dir / "provenance.json").read_text("utf-8"))
        target = codegen_doc or DOCS / "codegen-eval.md"
        _write(
            target,
            render_codegen(
                load_codegen(codegen_dir / "results.jsonl"),
                provenance["run"]["date"],
                test_only=False,
            ),
        )
        written.append(target)
    seed_calls = load_calls(replays / "codegen" / "calls.jsonl")
    for phase, target in (docs or REPAIR_DOCS).items():
        directory = repair_root / phase
        if not (directory / "results.jsonl").is_file():
            continue
        problems += verify_provenance(directory, repair=True)
        results = load_repair(directory / "results.jsonl")
        expected_path = replays / "repair" / phase / "expected.json"
        if not expected_path.is_file():
            problems.append(f"{phase}: results are committed but their replays are not")
            continue
        expected = json.loads(expected_path.read_text("utf-8"))
        calls = load_calls(replays / "repair" / phase / "calls.jsonl")
        problems += [f"{phase}: {p}" for p in cross_check(results, expected, calls, seed_calls)]
        provenance = json.loads((directory / "provenance.json").read_text("utf-8"))
        _write(
            target,
            render_repair_report(results, expected, calls, seed_calls, provenance, phase=phase),
        )
        written.append(target)
    return written, problems


def git_difference(paths: Sequence[Path]) -> int:
    """Exit code of ``git diff --exit-code`` for the files; a file git does not track fails."""
    names = [str(p.relative_to(REPO_ROOT)) for p in paths]
    for name in names:
        tracked = subprocess.run(  # noqa: S603
            ["git", "ls-files", "--error-unmatch", name],  # noqa: S607
            cwd=REPO_ROOT, capture_output=True, check=False,
        )  # fmt: skip
        if tracked.returncode:
            print(f"{name} is not tracked by git; commit the generated report", file=sys.stderr)
            return 1
    return subprocess.run(  # noqa: S603
        ["git", "diff", "--exit-code", "--", *names],  # noqa: S607
        cwd=REPO_ROOT, check=False,
    ).returncode  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    written, problems = render_all()
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    for path in written:
        print(f"wrote {path}")
    if problems:
        return 1
    return git_difference(written) if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
