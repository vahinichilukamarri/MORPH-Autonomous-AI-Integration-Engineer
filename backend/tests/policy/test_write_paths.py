"""Who can change what the policy trusts: only the approver-token REST routes.

An AST scan of ``app/`` shows that the functions which decide an approval, record system policy
attributes or write the ``SystemPolicyAttribute`` table are used by ``app/api/policy.py`` and by no
other module. The MCP server (M3) is covered by the same scan, so a tool cannot reach them either.
"""

import ast
from pathlib import Path

from sqlalchemy.orm import Session

from app.policy.loader import load_active
from app.policy.store import record_policy_version

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {Path("api/policy.py")}
OWNERS = {Path("policy/attributes.py"), Path("policy/approvals.py")}
GUARDED_NAMES = {"set_attributes", "SystemPolicyAttribute"}
GUARDED_ATTRIBUTES = {("approvals", "decide")}


def uses(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in GUARDED_NAMES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            if node.attr in GUARDED_NAMES:
                found.add(node.attr)
            if (
                isinstance(node.value, ast.Name)
                and (node.value.id, node.attr) in GUARDED_ATTRIBUTES
            ):
                found.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.alias) and node.name in GUARDED_NAMES:
            found.add(node.name)
    return found


def test_the_scan_sees_a_use_when_there_is_one() -> None:
    assert uses("attributes.set_attributes(s)") == {"set_attributes"}
    assert uses("from app.db_models import SystemPolicyAttribute") == {"SystemPolicyAttribute"}
    assert uses("approvals.decide(s, a, c, i)") == {"approvals.decide"}
    assert uses("x = 1") == set()


def test_only_the_approver_routes_can_write_attributes_or_decide_approvals() -> None:
    offenders: dict[str, set[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(APP)
        if relative in ALLOWED or relative in OWNERS or relative == Path("db_models.py"):
            continue
        hits = uses(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(relative)] = hits
    assert offenders == {}


def test_the_allowed_module_really_uses_them() -> None:
    hits = uses((APP / "api" / "policy.py").read_text(encoding="utf-8"))
    assert {"set_attributes", "approvals.decide"} <= hits


def test_a_policy_version_is_recorded_once_under_its_hash(session: Session) -> None:
    active = load_active()
    first = record_policy_version(session, active)
    second = record_policy_version(session, active)
    assert first.id == second.id and first.policy_hash == active.hash
    assert first.content["version"] == "v1" and len(first.content["rules"]) == 7
