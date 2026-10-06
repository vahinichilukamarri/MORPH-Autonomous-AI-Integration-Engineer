# ruff: noqa: E501  (the fixture tables below are intentionally one record per line)
"""Write deterministic sample records of each mock system to ``mock_systems/samples/``.

The records feed mapping validation (every proposal is executed on all of them) and appear, a
few at a time, in mapping prompts. They are valid per each system's own contract, and cover the
edge cases a mapping has to survive: null optional fields, every enum value, phone variants,
whitespace and multi-word names, epoch boundaries, and (for Support) users that were not
imported from the CRM.

Run from ``mock_systems/``: ``uv run python -m common.export_samples``.
"""

import json
from pathlib import Path
from typing import Any

from crm.models import Customer
from crm.store import CustomerStore
from support.models import User

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"

STATUS_TO_STATE = {"ACTIVE": "ENABLED", "INACTIVE": "DISABLED", "SUSPENDED": "BLOCKED"}
SEGMENT_TO_TIER = {"SMB": "STANDARD", "MIDMARKET": "STANDARD", "ENTERPRISE": "PRIORITY"}

# first_name, last_name, email, phone, status, segment, created_at
_EDGE_CUSTOMERS: list[tuple[str, Any, Any, str, Any, str, str, str]] = [
    ("C-1837", "Asha", "Verma", "asha.verma@example.com", "+919876543210", "ACTIVE", "MIDMARKET", "2024-03-05T10:15:30Z"),
    ("C-1001", "Kiran", None, "kiran@example.com", None, "INACTIVE", "SMB", "2023-01-01T00:00:00Z"),
    ("C-0042", " Ravi ", "Iyer ", "ravi.iyer@example.com", "+14155550123", "SUSPENDED", "ENTERPRISE", "1970-01-01T00:00:00Z"),
    ("C-2000", "Mary Ann", "Smith", "mary.ann@example.com", "+442071838750", "ACTIVE", "SMB", "2024-02-29T23:59:59Z"),
    ("C-777", "Priya", None, "priya@example.com", "+6591234567", "ACTIVE", "ENTERPRISE", "2022-12-31T23:59:59Z"),
    ("C-9999999", "Zoë", "Müller", "zoe@example.com", "+4915123456789", "INACTIVE", "MIDMARKET", "2025-06-15T12:00:00Z"),
    ("C-3", "Li", "Wei", "li.wei@example.com", None, "SUSPENDED", "SMB", "2021-07-04T08:30:00Z"),
]  # fmt: skip

# userId, externalRef, fullName, email, phone digits, state, tier, epoch seconds
_SUPPORT_ONLY_USERS: list[tuple[int, Any, str, str, Any, str, str, int]] = [
    (5001, None, "Mary Ann Smith", "mary.smith@example.com", "447700900123", "ENABLED", "STANDARD", 1717243200),
    (5002, None, "Priya", "priya.k@example.com", None, "BLOCKED", "PRIORITY", 1700000000),
    (5003, "C-5003", "Ana Lucia Gomez", "ana.gomez@example.com", "5511987654321", "DISABLED", "STANDARD", 1650000000),
]  # fmt: skip


def _customers() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seeded = CustomerStore().list(1, 6)[0]
    for customer in seeded:
        rows.append(customer.model_dump(mode="json"))
    for cid, first, last, email, phone, status, segment, created in _EDGE_CUSTOMERS:
        rows.append(
            Customer.model_validate(
                {
                    "customer_id": cid,
                    "first_name": first,
                    "last_name": last,
                    "email": email,
                    "phone": phone,
                    "status": status,
                    "segment": segment,
                    "created_at": created,
                }
            ).model_dump(mode="json")
        )
    return rows


def _epoch(iso: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _users(customers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Support users as the documented CRM -> Support conversion would produce them."""
    rows: list[dict[str, Any]] = []
    for c in customers:
        name = " ".join(f"{c['first_name']} {c['last_name'] or ''}".split())
        rows.append(
            {
                "userId": int(c["customer_id"].removeprefix("C-")),
                "externalRef": c["customer_id"],
                "fullName": name,
                "email_address": c["email"],
                "phoneNumber": c["phone"].removeprefix("+") if c["phone"] else None,
                "accountState": STATUS_TO_STATE[c["status"]],
                "tier": SEGMENT_TO_TIER[c["segment"]],
                "createdAt": _epoch(c["created_at"]),
            }
        )
    for uid, ref, name, email, phone, state, tier, created in _SUPPORT_ONLY_USERS:
        rows.append(
            {
                "userId": uid,
                "externalRef": ref,
                "fullName": name,
                "email_address": email,
                "phoneNumber": phone,
                "accountState": state,
                "tier": tier,
                "createdAt": created,
            }
        )
    return [User.model_validate(r).model_dump(mode="json") for r in rows]


def _rename(rows: list[dict[str, Any]], old: str, new: str) -> list[dict[str, Any]]:
    return [{(new if k == old else k): v for k, v in row.items()} for row in rows]


def current_samples() -> dict[str, dict[str, Any]]:
    customers = _customers()
    users = _users(customers)
    return {
        "crm_customer.v1.json": _document("crm", "Customer", "v1", customers),
        "crm_customer.v2.json": _document(
            "crm", "Customer", "v2", _rename(customers, "phone", "phone_number")
        ),
        "support_user.v1.json": _document("support", "User", "v1", users),
        "support_user.v2.json": _document(
            "support", "User", "v2", _rename(users, "tier", "serviceTier")
        ),
    }


def _document(
    system: str, entity: str, contract: str, records: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"system": system, "entity": entity, "contract": contract, "records": records}


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    SAMPLES_DIR.mkdir(exist_ok=True)
    for name, document in current_samples().items():
        (SAMPLES_DIR / name).write_text(render(document), encoding="utf-8", newline="\n")
        print(f"wrote {SAMPLES_DIR / name}")


if __name__ == "__main__":
    main()
