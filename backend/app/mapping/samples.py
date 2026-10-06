"""Sample source records (committed fixtures from the mock systems) for validation and prompts."""

import json
from pathlib import Path

from app.mapping.transform import JsonScalar

SAMPLE_FILES: dict[tuple[str, str], str] = {
    ("Customer", "1"): "crm_customer.v1.json",
    ("Customer", "2"): "crm_customer.v2.json",
    ("User", "1"): "support_user.v1.json",
    ("User", "2"): "support_user.v2.json",
}


def contract_major(api_version: str) -> str:
    """'2.0.0' -> '2'."""
    return api_version.split(".", 1)[0]


def load_samples(entity: str, api_version: str, samples_dir: Path) -> list[dict[str, JsonScalar]]:
    """The sample records for an entity at an API version; empty when there are none."""
    name = SAMPLE_FILES.get((entity, contract_major(api_version)))
    if name is None or not (samples_dir / name).is_file():
        return []
    document = json.loads((samples_dir / name).read_text(encoding="utf-8"))
    records: list[dict[str, JsonScalar]] = document["records"]
    return records
