import json
import re

from common.export_samples import SAMPLES_DIR, current_samples
from crm.models import Customer
from support.models import User


def test_committed_samples_match_the_generator() -> None:
    for name, document in current_samples().items():
        committed = json.loads((SAMPLES_DIR / name).read_text(encoding="utf-8"))
        assert committed == document, (
            f"{name} is stale: run `uv run python -m common.export_samples`"
        )


def test_v1_samples_satisfy_their_system_contracts() -> None:
    crm = json.loads((SAMPLES_DIR / "crm_customer.v1.json").read_text(encoding="utf-8"))
    support = json.loads((SAMPLES_DIR / "support_user.v1.json").read_text(encoding="utf-8"))
    for record in crm["records"]:
        Customer.model_validate(record)
    for record in support["records"]:
        User.model_validate(record)


def test_samples_cover_the_edge_cases_a_mapping_must_survive() -> None:
    crm = current_samples()["crm_customer.v1.json"]["records"]
    assert {r["status"] for r in crm} == {"ACTIVE", "INACTIVE", "SUSPENDED"}
    assert {r["segment"] for r in crm} == {"SMB", "MIDMARKET", "ENTERPRISE"}
    assert any(r["last_name"] is None for r in crm)
    assert any(r["phone"] is None for r in crm)
    prefixes = {re.match(r"\+\d{1,3}", r["phone"]).group(0)[:3] for r in crm if r["phone"]}  # type: ignore[union-attr]
    assert len(prefixes) >= 4, "several country codes"
    assert any(r["first_name"] != r["first_name"].strip() for r in crm), "stray whitespace"
    assert any(r["customer_id"].startswith("C-0") for r in crm), "leading zeros in the id"
    assert any(" " in r["first_name"].strip() for r in crm), "multi-word first name"
    assert len({r["customer_id"] for r in crm}) == len(crm)

    support = current_samples()["support_user.v1.json"]["records"]
    assert {r["accountState"] for r in support} == {"ENABLED", "DISABLED", "BLOCKED"}
    assert {r["tier"] for r in support} == {"STANDARD", "PRIORITY"}
    assert any(r["externalRef"] is None for r in support), "users not imported from the CRM"
    assert any(r["phoneNumber"] is None for r in support)
    assert any(r["createdAt"] == 0 for r in support), "epoch boundary"
    assert len({r["userId"] for r in support}) == len(support)


def test_v2_samples_use_the_renamed_fields() -> None:
    samples = current_samples()
    crm_v2 = samples["crm_customer.v2.json"]["records"]
    assert all("phone_number" in r and "phone" not in r for r in crm_v2)
    support_v2 = samples["support_user.v2.json"]["records"]
    assert all("serviceTier" in r and "tier" not in r for r in support_v2)
    assert len(crm_v2) == len(samples["crm_customer.v1.json"]["records"])
