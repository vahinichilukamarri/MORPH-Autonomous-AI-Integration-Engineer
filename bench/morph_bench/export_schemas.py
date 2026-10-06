"""Write JSON Schemas for the scenario and answer-key formats to ``bench/schemas/``.

Run from ``bench/``: ``uv run python -m morph_bench.export_schemas``.
"""

import json
from pathlib import Path
from typing import Any

from morph_bench.models import AnswerKey, Scenario

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"


def current_schemas() -> dict[str, dict[str, Any]]:
    return {
        "scenario.schema.json": Scenario.model_json_schema(),
        "answer_key.schema.json": AnswerKey.model_json_schema(),
    }


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2) + "\n"


def main() -> None:
    SCHEMAS_DIR.mkdir(exist_ok=True)
    for name, schema in current_schemas().items():
        (SCHEMAS_DIR / name).write_text(render(schema), encoding="utf-8", newline="\n")
        print(f"wrote {SCHEMAS_DIR / name}")


if __name__ == "__main__":
    main()
