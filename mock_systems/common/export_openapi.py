"""Write the clean (v1) OpenAPI contract of each mock system to ``mock_systems/openapi/``.

Run from ``mock_systems/``: ``uv run python -m common.export_openapi``.
The bench answer-key validator reads these files, and a test fails when they go stale.
"""

import json
from pathlib import Path
from typing import Any

from crm.main import create_app as create_crm_app
from support.main import create_app as create_support_app

OPENAPI_DIR = Path(__file__).resolve().parents[1] / "openapi"


def current_specs() -> dict[str, dict[str, Any]]:
    return {
        "crm.v1.json": create_crm_app().openapi(),
        "support.v1.json": create_support_app().openapi(),
    }


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    OPENAPI_DIR.mkdir(exist_ok=True)
    for name, spec in current_specs().items():
        (OPENAPI_DIR / name).write_text(render(spec), encoding="utf-8", newline="\n")
        print(f"wrote {OPENAPI_DIR / name}")


if __name__ == "__main__":
    main()
