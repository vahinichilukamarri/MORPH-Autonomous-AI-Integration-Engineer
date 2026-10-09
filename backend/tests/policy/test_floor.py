"""The floor's argument checks: URLs, paths (with symlinks), forbidden names, size, test data."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.policy.floor import (
    inspect_arguments,
    looks_like_url,
    oversized,
    path_escapes,
    strings_in,
    withhold_test_data,
)
from app.policy.models import DataClass
from app.policy.toolspec import TOOL_SPECS, forbidden_names


@pytest.fixture
def root(tmp_path: Path) -> Path:
    spec_root = tmp_path / "specs"
    spec_root.mkdir()
    (spec_root / "crm.json").write_text("{}", encoding="utf-8")
    (spec_root / "sub").mkdir()
    (spec_root / "sub" / "a.json").write_text("{}", encoding="utf-8")
    return spec_root


def make_link(link: Path, target: Path) -> None:
    """A symlink, or on Windows without the privilege a directory junction."""
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        return
    except OSError:
        if sys.platform != "win32" or not target.is_dir():
            pytest.skip("this host cannot create a symlink and no junction fallback applies")
    done = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if done.returncode:
        pytest.skip("this host cannot create a symlink or a junction")


@pytest.mark.parametrize("value", ["crm.json", "sub/a.json", "sub\\a.json", "./crm.json"])
def test_a_file_inside_the_root_is_fine(root: Path, value: str) -> None:
    assert not path_escapes(root, value)


@pytest.mark.parametrize(
    "value",
    [
        "../crm.json", "sub/../../x.json", "..\\x.json", "sub/..", "/etc/passwd", "\\windows\\x",
        "C:\\Windows\\System32\\x.json", "c:/x.json", "\\\\server\\share\\x.json",
        "a\x00b.json", "", "  ",
    ],
)  # fmt: skip
def test_a_path_that_could_leave_the_root_is_refused(root: Path, value: str) -> None:
    assert path_escapes(root, value)


def test_a_symlink_to_a_file_outside_the_root_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = root / "link.json"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("this host cannot create a file symlink")
    assert path_escapes(root, "link.json")


def test_a_symlink_or_junction_to_a_directory_outside_the_root_is_refused(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text("{}", encoding="utf-8")
    make_link(root / "escape", outside)
    assert path_escapes(root, "escape/secret.json")


def test_a_symlink_that_stays_inside_the_root_is_fine(root: Path) -> None:
    make_link(root / "alias", root / "sub")
    assert not path_escapes(root, "alias/a.json")


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/spec.json", "https://x/y", "HTTP://X", "file:///etc/passwd",
        "ftp://x/y", "http://127.0.0.1:8101/openapi.json", "gopher://x", "//evil.example/x",
        " https://x ", "\\\\evil\\share",
    ],
)  # fmt: skip
def test_urls_are_recognised(value: str) -> None:
    assert looks_like_url(value)


@pytest.mark.parametrize("value", ["crm.v1.json", "specs/crm.json", "a-b_c.json", "Customer", ""])
def test_ordinary_names_are_not_urls(value: str) -> None:
    assert not looks_like_url(value)


def test_the_facts_are_computed_from_every_string_argument(root: Path) -> None:
    facts = inspect_arguments({"file": "crm.json", "name": "http://x"}, spec_root=root)
    assert facts.url_in_arguments and not facts.path_escapes_root
    facts = inspect_arguments({"file": "../x.json", "name": "crm"}, spec_root=root)
    assert facts.path_escapes_root and not facts.url_in_arguments
    nested = list(strings_in({"a": [1, {"b": "x"}, ("y",)], "c": None}))
    assert nested == ["x", "y"]


def test_no_registered_tool_has_a_forbidden_name() -> None:
    assert forbidden_names([t.name for t in TOOL_SPECS]) == []
    assert len(TOOL_SPECS) == 13


@pytest.mark.parametrize(
    "name",
    [
        "approve_mapping",
        "override_mapping",
        "decide_approval",
        "update_policy",
        "set_policy",
        "set_policy_attributes",
        "grade_integration",
        "run_oracle",
        "bench_run",
        "http_get",
        "fetch_url",
        "run_shell",
        "execute_sync",
        "read_file",
        "read_env",
        "get_secret",
        "get_token",
        "admin_reset",
        "download_spec",
        "answer_key_lookup",
    ],
)
def test_the_forbidden_capabilities_are_recognised(name: str) -> None:
    assert forbidden_names([name]) == [name]


def test_size_and_test_data_rules() -> None:
    assert oversized({"x": "a" * 2_000}, 1_000) and not oversized({"x": "a"}, 1_000)
    files = [{"path": "integration/sync.py", "c": 1}, {"path": "tests_generated/cases.py", "c": 2}]
    kept, withheld = withhold_test_data(files, None)
    assert [f["path"] for f in kept] == ["integration/sync.py"]
    assert withheld == ["tests_generated/cases.py"]
    for data_class in (DataClass.INTERNAL, DataClass.RESTRICTED, DataClass.UNCLASSIFIED):
        assert withhold_test_data(files, data_class)[1] == ["tests_generated/cases.py"]
    assert withhold_test_data(files, DataClass.SYNTHETIC) == (files, [])
