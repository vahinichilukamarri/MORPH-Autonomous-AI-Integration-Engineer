"""A process that appends audit events to a shared chain, for the two-process concurrency test.

    python -m tests.policy.audit_worker <database url> <chain> <count> <start epoch seconds> <label>

It waits for the agreed start time so that two workers really overlap, then appends ``count``
events. It prints how many it wrote. It never prints the database URL.
"""

import sys
import time

from sqlalchemy import create_engine

from app.policy.audit import AuditLog, EventType


def main(argv: list[str]) -> int:
    url, chain, count, start, label = argv[0], argv[1], int(argv[2]), float(argv[3]), argv[4]
    engine = create_engine(url)
    log = AuditLog(engine, chain=chain)
    while time.time() < start:
        time.sleep(0.005)
    for n in range(count):
        log.append(
            EventType.CALL_RECEIVED, session_id=f"worker-{label}", principal=f"agent:{label}",
            tool="get_system", call_id=f"{label}-{n}", payload={"n": n, "label": label},
        )  # fmt: skip
    engine.dispose()
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
