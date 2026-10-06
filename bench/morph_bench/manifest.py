"""Checksum manifest of the hand-written fixtures (scenarios, answer keys, reference pipelines).

Answer keys are ground truth. Any change to them must be deliberate, so their SHA-256 sums are
committed in ``scenarios/MANIFEST.sha256`` and a test fails when a file differs. Updating the
manifest is an explicit command::

    uv run python -m morph_bench.manifest --update
"""

import argparse
import hashlib
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1]
MANIFEST = BENCH_DIR / "scenarios" / "MANIFEST.sha256"
FIXTURE_GLOBS = ("scenarios/*/scenario.yaml", "scenarios/*/answer_key.yaml", "references/*.yaml")


def _digest(path: Path) -> str:
    # Line endings do not count as a change.
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def compute(root: Path = BENCH_DIR) -> dict[str, str]:
    paths = sorted({p for pattern in FIXTURE_GLOBS for p in root.glob(pattern)})
    return {p.relative_to(root).as_posix(): _digest(p) for p in paths}


def render(entries: dict[str, str]) -> str:
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(entries.items()))


def read(manifest: Path = MANIFEST) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split("  ", 1)
            entries[name] = digest
    return entries


def verify(root: Path = BENCH_DIR, manifest: Path = MANIFEST) -> list[str]:
    """Differences between the committed manifest and the files on disk (empty when clean)."""
    expected, actual = read(manifest), compute(root)
    problems = [
        f"changed: {n}" for n in sorted(expected.keys() & actual.keys()) if expected[n] != actual[n]
    ]
    problems += [f"missing: {n}" for n in sorted(expected.keys() - actual.keys())]
    problems += [f"not in manifest: {n}" for n in sorted(actual.keys() - expected.keys())]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--update", action="store_true", help="rewrite the manifest (deliberate)")
    args = parser.parse_args(argv)
    if args.update:
        MANIFEST.write_text(render(compute()), encoding="utf-8", newline="\n")
        print(f"wrote {MANIFEST}")
        return 0
    problems = verify()
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
