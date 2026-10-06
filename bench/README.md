# MORPH-Bench

The fixtures MORPH is graded against. Each scenario is a folder in `scenarios/`:

```
scenarios/<id>/
  scenario.yaml     what to integrate, the requirement in plain language, the fault profile
  answer_key.yaml   the correct field-level mapping, as input -> output examples (never code)
```

Both files are hand-written and **read-only for every agent**. The repair agent may change
generated integration code only; it never writes scenarios, answer keys or fixtures. Oracle
tests (v0.4) are built deterministically from the contracts plus the answer key.

## Format

- Mapping types: `DIRECT`, `COMPOSITE`, `TRANSFORMATION`, `CONSTANT`, `DERIVED`, `UNRESOLVED`.
  `DERIVED` means the target value comes from a different concept (segment -> tier), while
  `TRANSFORMATION` re-encodes the same concept (format, type or vocabulary).
- Every field of the target entity needs exactly one entry. `UNRESOLVED` entries carry a
  `reason` and no examples.
- The models live in `morph_bench/models.py`; JSON Schemas are exported to `schemas/`.

## Validation

`morph_bench.loader.load_bundle` checks a scenario against the real OpenAPI contracts in
`mock_systems/openapi/`: every target field is covered, every source field exists, and every
example input and output satisfies the contract of its field. All problems are reported at
once.

## Commands (PowerShell, from `bench/`)

```powershell
uv run pytest -q
uv run python -m morph_bench.export_schemas
```
