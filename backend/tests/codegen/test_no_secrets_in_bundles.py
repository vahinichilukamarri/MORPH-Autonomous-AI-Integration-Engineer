"""A generated bundle never contains secrets or environment values.

Bundles are mounted into the sandbox and, on a Linux host, made world-readable so the sandbox
user can read them. Anything secret in a bundle would therefore be readable by every local user
and by the generated code itself. Credentials and URLs only ever arrive at run time, through
MORPH_* environment variables of the sandbox, never through the bundle.
"""

import json
import os
import re
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.codegen.sandbox import make_readable
from app.codegen.service import GENERATED, generate_from_input, materialize
from app.llm.fake import ScriptedFakeProvider
from tests.codegen.fixtures import persist_run, s1_input
from tests.codegen.test_llm_codegen import GOOD, L2_GOOD, l2_reply

SENTINELS = {
    "GROQ_API_KEY": "gsk_sentinel_0123456789abcdefghij",
    "DATABASE_URL": "postgresql+psycopg://leak:sentinel-db-password@db.internal:5432/x",
    "MORPH_SOURCE_CREDENTIAL": "sentinel-source-credential-4f9a",
    "MORPH_TARGET_CREDENTIAL": "sentinel-target-credential-77c1",
    "MORPH_SOURCE_URL": "http://sentinel-source.internal:8101",
    "MORPH_TARGET_URL": "http://sentinel-target.internal:8102",
    "CRM_API_KEY": "sentinel-crm-key-aa11",
    "SUPPORT_TOKEN": "sentinel-support-token-bb22",
    "ADMIN_TOKEN": "sentinel-admin-token-cc33",
}
SECRET_SHAPES = re.compile(
    r"gsk_[A-Za-z0-9]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9]{12,}|://[^/\s]*:[^@/\s]+@"
)


def stored(session: Session):  # type: ignore[no-untyped-def]
    inp = s1_input()
    run_id = persist_run(
        session, source=("crm.v1", "crm"), target=("support.v1", "support"),
        source_entity="Customer", target_entity="User", fields=inp.fields,
    )  # fmt: skip
    return replace(inp, mapping_run_id=run_id)


@pytest.mark.parametrize("condition", ["D", "L1", "L2"])
def test_no_bundle_file_holds_a_secret_or_an_environment_value(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    llm = {
        "D": None,
        "L1": ScriptedFakeProvider(replies=[json.dumps(GOOD)]),
        "L2": ScriptedFakeProvider(replies=[l2_reply(L2_GOOD)]),
    }[condition]
    version = generate_from_input(session, stored(session), condition=condition, llm=llm)
    assert version.status == GENERATED
    bundle = materialize(version, tmp_path / "bundle")
    make_readable(bundle)
    texts = {
        p.relative_to(bundle).as_posix(): p.read_text(encoding="utf-8")
        for p in bundle.rglob("*")
        if p.is_file()
    }
    assert "manifest.json" in texts and "integration/clients.py" in texts
    leaks = [
        (path, name)
        for path, text in texts.items()
        for name, value in SENTINELS.items()
        if value in text
    ]
    assert leaks == []
    shaped = [path for path, text in texts.items() if SECRET_SHAPES.search(text)]
    assert shaped == []
    # credentials are only ever named, never held: the generated clients read them at run time
    clients = texts["integration/clients.py"]
    assert 'env("MORPH_SOURCE_CREDENTIAL")' in clients and "://" not in clients


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_files_are_readable_but_never_writable_or_executable_by_others(
    session: Session, tmp_path: Path
) -> None:
    version = generate_from_input(session, stored(session))
    bundle = materialize(version, tmp_path / "bundle")
    make_readable(bundle)
    for path in [bundle, *bundle.rglob("*")]:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode & stat.S_IROTH, path
        assert not mode & (stat.S_IWOTH | stat.S_IWGRP), path
        if path.is_file():
            assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), path
