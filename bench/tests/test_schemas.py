import json

from morph_bench.export_schemas import SCHEMAS_DIR, current_schemas


def test_committed_json_schemas_are_current() -> None:
    for name, schema in current_schemas().items():
        committed = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
        assert committed == schema, f"{name} is stale: run export_schemas"
