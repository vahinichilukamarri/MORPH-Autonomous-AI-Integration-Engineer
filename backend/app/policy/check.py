"""Check the policy directory: ``python -m app.policy.check``. Exit code 1 on any problem.

Run in CI. It verifies that the active policy matches ``policy.lock``, that every released version
still hashes to ``versions.json``, that the floor is not weakened, and that a version which loosens
relative to its predecessor says so.
"""

import sys
from collections.abc import Sequence
from pathlib import Path

from app.policy.loader import POLICY_DIR, PolicyError, check_policy_dir, load_active


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    directory = Path(args[0]) if args else POLICY_DIR
    problems = check_policy_dir(directory)
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    if problems:
        return 1
    try:
        active = load_active(directory)
    except PolicyError as error:  # unreachable after a clean check, kept as a guard
        print(f"problem: {error}", file=sys.stderr)
        return 1
    print(f"policy {active.version} ok ({active.hash[:12]}, {len(active.file.rules)} rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
