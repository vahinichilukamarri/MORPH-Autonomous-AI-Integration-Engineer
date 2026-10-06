"""The first answer key must agree with an independent, test-only reference of the rules."""

import re
from datetime import UTC, datetime
from typing import Any

from morph_bench.loader import SCENARIOS_ROOT, load_bundle

SCENARIO_DIR = SCENARIOS_ROOT / "crm_customer_to_support_user"
STATUS = {"ACTIVE": "ENABLED", "INACTIVE": "DISABLED", "SUSPENDED": "BLOCKED"}


def reference(target_field: str, source: dict[str, Any]) -> Any:
    match target_field:
        case "userId":
            found = re.fullmatch(r"C-(\d+)", source["customer_id"])
            assert found
            return int(found.group(1))
        case "externalRef":
            return source["customer_id"]
        case "fullName":
            parts = [source["first_name"], source["last_name"] or ""]
            return " ".join(" ".join(parts).split())
        case "email_address":
            return source["email"]
        case "phoneNumber":
            return None if source["phone"] is None else source["phone"].removeprefix("+")
        case "accountState":
            return STATUS[source["status"]]
        case "tier":
            return "PRIORITY" if source["segment"] == "ENTERPRISE" else "STANDARD"
        case "createdAt":
            moment = datetime.fromisoformat(source["created_at"].replace("Z", "+00:00"))
            return int(moment.astimezone(UTC).timestamp())
    raise AssertionError(f"no reference for {target_field}")


def test_every_example_matches_the_reference_rules() -> None:
    bundle = load_bundle(SCENARIO_DIR)
    checked = 0
    for entry in bundle.answer_key.mappings:
        for example in entry.examples:
            assert reference(entry.target_field, example.input) == example.output, (
                entry.target_field
            )
            checked += 1
    assert checked >= 8


def test_mapping_types_cover_the_documented_mismatches() -> None:
    bundle = load_bundle(SCENARIO_DIR)
    types = {m.target_field: m.mapping_type.value for m in bundle.answer_key.mappings}
    assert types == {
        "userId": "TRANSFORMATION",
        "externalRef": "DIRECT",
        "fullName": "COMPOSITE",
        "email_address": "DIRECT",
        "phoneNumber": "TRANSFORMATION",
        "accountState": "TRANSFORMATION",
        "tier": "DERIVED",
        "createdAt": "TRANSFORMATION",
    }


def test_scenario_uses_the_clean_v1_contract() -> None:
    assert load_bundle(SCENARIO_DIR).scenario.fault_profile.contract_version == "v1"
