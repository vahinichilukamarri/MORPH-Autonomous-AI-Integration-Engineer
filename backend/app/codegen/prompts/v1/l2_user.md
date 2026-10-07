## Task

Write the synchronisation module `integration/sync.py` for copying records from the source system to the target system, using the runtime library described below. The field mappings are already compiled into `integration.transform.to_target`, and both HTTP clients already exist in `integration.clients`; you write only how records are read, matched and written.

Return the complete Python source of the module in the `source` field and a one-sentence `notes`.

## What the module must define (trusted)

```python
def run(keys: tuple[str, ...]) -> RunReport: ...
```

`keys` are ids supplied by the operator; use them when the source cannot be listed, ignore them otherwise. `run` must return a `RunReport` with one `RecordResult` per source record it handled, and must set `report.requests` to the request counts of the two clients.

Behaviour that is required:
- Every source record ends in exactly one outcome: CREATED, UPDATED, UNCHANGED, NOT_SYNCABLE or FAILED.
- Look the target record up before writing, and do not write when the target already holds the mapped values, so that running twice writes nothing the second time.
- Never invent an identifier. A record without a usable identity is NOT_SYNCABLE.
- An authentication failure ends the run (`report.fail_run(...)`); do not keep calling a system that rejected the credentials.
- A source field the mapping needs that is absent from a source record, or a required field absent from a target record, is contract drift: end the run with `Category.CONTRACT_DRIFT` and write nothing for that record. Absent is not null.
- One failing record must not stop the others; record it as FAILED with its category and, for validation errors, the field names.
- Keep every loop bounded.

## Rules for the code (checked by a static gate before anything runs)

- Import only from `morph_runtime`, `integration` and these standard modules: `typing`, `dataclasses`, `re`, `datetime`, `json`, `enum`, `collections.abc`, `__future__`.
- No `eval`, `exec`, `compile`, `open`, `getattr`, `setattr`, `globals`, `__import__`, no attribute or name starting with two underscores, no decorators except `dataclass`, no `async`, no `global` or `nonlocal`, no comments that suppress lint or type checks, and no string that looks like a URL or a secret.
- The code must pass `ruff` and `mypy --strict`: annotate everything.

## Runtime library (trusted reference)

{{RUNTIME_API}}

{{SOURCE_BLOCK}}

{{TARGET_BLOCK}}

{{MAPPING_BLOCK}}
