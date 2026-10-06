import json

from common.export_openapi import OPENAPI_DIR, current_specs


def test_committed_openapi_specs_match_the_running_apps() -> None:
    for name, spec in current_specs().items():
        committed = json.loads((OPENAPI_DIR / name).read_text(encoding="utf-8"))
        assert committed == spec, f"{name} is stale: run `uv run python -m common.export_openapi`"
