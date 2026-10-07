"""Bulk records for the pagination checks, with expected values computed by independent code.

These functions are written directly from the answer keys and deliberately share nothing with the
mapping DSL, the compiler or the generated code, so they can disagree with them.
"""

import calendar
import time
from typing import Any

_STATE = {"ACTIVE": "ENABLED", "INACTIVE": "DISABLED", "SUSPENDED": "BLOCKED"}
_REVERSE_STATE = {v: k for k, v in _STATE.items()}
_STATUSES = ("ACTIVE", "INACTIVE", "SUSPENDED")
_SEGMENTS = ("SMB", "MIDMARKET", "ENTERPRISE")


def _iso(i: int) -> str:
    # a different calendar day for every record, starting 2020-01-01
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1577836800 + i * 86400 + i))


def customer(i: int) -> dict[str, Any]:
    """The i-th bulk CRM customer."""
    return {
        "customer_id": f"C-{10000 + i}",
        "first_name": f"First{i}",
        "last_name": None if i % 7 == 0 else f"Last{i}",
        "email": f"bulk{i}@example.com",
        "phone": None if i % 5 == 0 else f"+9198{i:08d}",
        "status": _STATUSES[i % 3],
        "segment": _SEGMENTS[i % 3],
        "created_at": _iso(i),
    }


def support_user_for(c: dict[str, Any]) -> dict[str, Any]:
    """What Support must hold for CRM customer ``c`` (from the S1 answer key)."""
    name = c["first_name"] if not c["last_name"] else f"{c['first_name']} {c['last_name']}"
    return {
        "userId": int(c["customer_id"][2:]),
        "externalRef": c["customer_id"],
        "fullName": name,
        "email_address": c["email"],
        "phoneNumber": None if c["phone"] is None else c["phone"][1:],
        "accountState": _STATE[c["status"]],
        "tier": "PRIORITY" if c["segment"] == "ENTERPRISE" else "STANDARD",
        "createdAt": calendar.timegm(time.strptime(c["created_at"], "%Y-%m-%dT%H:%M:%SZ")),
    }


def support_user(i: int) -> dict[str, Any]:
    """The i-th bulk Support user, each pointing at a CRM customer (see ``crm_customer_for``)."""
    return {
        "userId": 20000 + i,
        "externalRef": f"C-{30000 + i}",
        "fullName": f"Given{i} Family{i}",
        "email_address": f"user{i}@example.com",
        "phoneNumber": f"9198{i:08d}",
        "accountState": "ENABLED" if i % 2 else "DISABLED",
        "tier": "STANDARD",
        "createdAt": 1700000000 + i,
    }


def crm_customer_for(i: int) -> dict[str, Any]:
    """The CRM customer that bulk user ``i`` refers to, before the sync."""
    return {
        "customer_id": f"C-{30000 + i}",
        "first_name": "Old",
        "last_name": "Name",
        "email": f"old{i}@example.com",
        "phone": None,
        "status": "ACTIVE",
        "segment": _SEGMENTS[i % 3],
        "created_at": "2022-01-01T00:00:00Z",
    }


def crm_customer_after(i: int) -> dict[str, Any]:
    """The same customer after the sync (answer key of support_user_to_crm_customer)."""
    user = support_user(i)
    return {
        **crm_customer_for(i),
        "first_name": f"Given{i}",
        "last_name": f"Family{i}",
        "email": user["email_address"],
        "phone": "+" + user["phoneNumber"],
        "status": _REVERSE_STATE[user["accountState"]],
    }
