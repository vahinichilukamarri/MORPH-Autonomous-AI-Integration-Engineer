"""The demo script runs against a database and tells the story it claims to tell."""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import Engine

BACKEND = Path(__file__).resolve().parents[2]


def test_the_demo_walks_through_refusals_a_human_step_and_a_valid_chain(
    test_engine: Engine,
) -> None:
    env = {**os.environ, "DATABASE_URL": test_engine.url.render_as_string(hide_password=False)}
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "scripts.mcp_demo"], cwd=BACKEND, env=env, capture_output=True,
        text=True, timeout=300, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-2000:]
    out = done.stdout
    assert "ingest_contract (reader)" in out and "ROLE_NOT_PERMITTED" in out
    assert "UNKNOWN_TOOL" in out and "PATH_ESCAPE" in out and "URL_DENIED" in out
    assert "environment=mock data_class=synthetic" in out
    assert "ok=True" in out
