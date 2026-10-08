"""The repair graph with the real sandbox: the v0.4 gate, the generated tests and the smoke test.

The model is still scripted. Marker: docker.
"""

from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.codegen.sandbox import SandboxRunner
from app.db_models import GateResultRow, IntegrationVersion, RepairAttempt, SandboxRunRow
from app.repair.service import run_repair
from app.repair.smoke import make_smoke_runner
from tests.repair.support import GOOD, SUPPRESSED, l2_reply, make_rig

pytestmark = pytest.mark.docker


def rows_of(session: Session, run_id: int) -> list[RepairAttempt]:
    session.expire_all()
    return list(
        session.scalars(
            select(RepairAttempt)
            .where(RepairAttempt.repair_run_id == run_id)
            .order_by(RepairAttempt.attempt)
        )
    )


def test_ready_at_attempt_two_through_the_real_gate_tests_and_smoke(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    rig = make_rig(
        session, test_engine, tmp_path,
        ["this is not json", l2_reply(SUPPRESSED), l2_reply(GOOD)],
        runner=SandboxRunner(), smoke_runner=make_smoke_runner(),
    )  # fmt: skip
    result = run_repair(rig.env)
    assert (result.status, result.attempts) == ("READY", 3), result
    rows = rows_of(session, result.run_id)
    final = session.get(IntegrationVersion, rows[2].integration_version_id)
    assert final is not None and final.status == "READY"
    stages = session.scalars(
        select(GateResultRow).where(GateResultRow.integration_version_id == final.id)
    ).all()
    assert [(g.stage, g.passed) for g in stages] == [("ast", True), ("ruff", True), ("mypy", True)]
    purposes = session.scalars(
        select(SandboxRunRow.purpose).where(SandboxRunRow.integration_version_id == final.id)
    ).all()
    assert purposes == ["generated_tests"]


def test_ruff_and_mypy_findings_become_feedback_and_a_clean_module_follows(
    session: Session, test_engine: Engine, tmp_path: Path
) -> None:
    unused = GOOD.replace("from typing import Any\n", "import re\nfrom typing import Any\n")
    mistyped = GOOD.replace("PAGE_SIZE = 100\n", 'PAGE_SIZE = "100"\n')
    assert unused != GOOD and mistyped != GOOD
    rig = make_rig(
        session, test_engine, tmp_path,
        [l2_reply(unused), l2_reply(mistyped), l2_reply(GOOD)],
        runner=SandboxRunner(), smoke_runner=make_smoke_runner(),
    )  # fmt: skip
    result = run_repair(rig.env)
    rows = rows_of(session, result.run_id)
    assert result.status == "READY", [r.feedback for r in rows]
    assert [r.failed_stage for r in rows] == ["RUFF", "MYPY", None]
    ruff = rows[0].feedback["items"][0]  # type: ignore[index]
    assert (ruff["stage"], ruff["code"], ruff["file"]) == ("RUFF", "F401", "integration/sync.py")
    mypy = rows[1].feedback["items"][0]  # type: ignore[index]
    assert (mypy["stage"], mypy["code"]) == ("MYPY", "TYPE_ERROR")
    assert "/" not in mypy["message"][:2] and "\\" not in mypy["message"]
