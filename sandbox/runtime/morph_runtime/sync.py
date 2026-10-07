"""The sync engine: reads source records, maps them, and writes them idempotently to the target.

Hand-written and trusted. A generated integration only supplies a ``Strategy`` (which operations
to call, which field is the identity, what is writable) and the compiled ``to_target`` function.

Behaviour, in short:

* every source record ends in exactly one outcome: CREATED, UPDATED, UNCHANGED, NOT_SYNCABLE or
  FAILED (with a category and, for validation errors, the field names);
* an existing target record is looked up first (by the mapped identity, then by a natural key), and
  nothing is written when the target already holds the mapped values, so a second run issues no
  write requests;
* authentication failures stop the run at once; contract drift (a source field the mapping needs
  is missing) stops the run before anything is written for that record;
* a bounded number of consecutive failures or the run time budget ends the run cleanly with the
  partial report.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from morph_runtime.errors import Category, RuntimeFailure
from morph_runtime.http import HttpClient
from morph_runtime.ops import FieldTransformError, JsonScalar, MissingFieldError, Record
from morph_runtime.paging import paginate
from morph_runtime.report import Outcome, RecordResult, RunReport
from morph_runtime.retry import Clock, SystemClock

CONSECUTIVE_FAILURE_LIMIT = 8
DEFAULT_RUN_BUDGET_S = 50.0
_AMBIGUOUS = frozenset({Category.SERVER, Category.TIMEOUT, Category.MALFORMED_RESPONSE})
_TRANSIENT = frozenset(
    {
        Category.SERVER,
        Category.TIMEOUT,
        Category.NETWORK,
        Category.RATE_LIMIT,
        Category.MALFORMED_RESPONSE,
    }
)


class SourceMode(StrEnum):
    LIST = "LIST"  # the source has a paginated list operation
    KEYS = "KEYS"  # no list operation: the operator supplies the record ids


class TargetMode(StrEnum):
    UPSERT = "UPSERT"  # PUT /things/{id} creates or replaces
    CREATE_UPDATE = "CREATE_UPDATE"  # POST to create, PATCH or PUT /things/{id} to update


@dataclass(frozen=True)
class SourceSpec:
    mode: SourceMode
    key_field: str
    list_path: str = ""
    page_param: str = "page"
    size_param: str = "page_size"
    page_size: int = 100
    items_key: str = "items"
    total_key: str | None = None
    get_path: str = ""  # "/users/{id}" (KEYS mode)


@dataclass(frozen=True)
class TargetSpec:
    mode: TargetMode
    id_field: str  # the mapped target field holding the target's identity
    get_path: str  # "/users/{id}"
    update_method: str  # "PUT" or "PATCH"
    update_path: str  # "/users/{id}"
    create_path: str = ""  # POST collection (CREATE_UPDATE)
    create_fields: tuple[str, ...] = ()  # writable on create
    update_fields: tuple[str, ...] = ()  # writable on update
    create_only: tuple[str, ...] = ()  # sent on create, never on update (constants)
    omit_if_null: tuple[str, ...] = ()  # optional, non-nullable fields: omitted when null
    natural_key: str | None = None  # fallback identity when the id is assigned by the target
    id_assigned_by_target: bool = False
    list_path: str = ""  # target list operation, used to index the natural key
    page_param: str = "page"
    size_param: str = "page_size"
    page_size: int = 100
    items_key: str = "items"
    total_key: str | None = None


@dataclass(frozen=True)
class Strategy:
    source: SourceSpec
    target: TargetSpec


class _Fatal(Exception):
    def __init__(self, category: Category, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


@dataclass(frozen=True)
class _Item:
    key: str | None
    record: Record | None
    failure: RuntimeFailure | None = None


def _path(template: str, identity: object) -> str:
    return template.replace("{id}", quote(str(identity), safe=""))


def _norm(value: object) -> str:
    return str(value).strip().lower()


class SyncEngine:
    def __init__(
        self,
        strategy: Strategy,
        source: HttpClient,
        target: HttpClient,
        to_target: Callable[[Record], dict[str, JsonScalar]],
        *,
        keys: tuple[str, ...] = (),
        clock: Clock | None = None,
        budget_s: float = DEFAULT_RUN_BUDGET_S,
    ) -> None:
        self.strategy = strategy
        self.source = source
        self.target = target
        self.to_target = to_target
        self.keys = keys
        self.clock = clock or SystemClock()
        self.budget_s = budget_s
        self._index_cache: dict[str, list[dict[str, Any]]] | None = None
        self._consecutive_failures = 0

    # ---- run -----------------------------------------------------------------------------

    def run(self) -> RunReport:
        report = RunReport()
        started = self.clock.now()
        try:
            for item in self._source_items():
                if self.clock.now() - started > self.budget_s:
                    raise _Fatal(Category.TIMEOUT, "run time budget exhausted")
                self._sync_item(item, report)
        except _Fatal as stop:
            report.fail_run(stop.category, stop.detail)
        except RuntimeFailure as failure:
            report.fail_run(failure.category, failure.detail)
        report.requests = {
            "source": self.source.requests_made,
            "target": self.target.requests_made,
            "source_retries": self.source.retries_made,
            "target_retries": self.target.retries_made,
        }
        return report

    # ---- source --------------------------------------------------------------------------

    def _source_items(self) -> Iterator[_Item]:
        spec = self.strategy.source
        if spec.mode is SourceMode.KEYS:
            for key in self.keys:
                try:
                    record = self.source.request("GET", _path(spec.get_path, key))
                except RuntimeFailure as failure:
                    if failure.category is Category.AUTH:
                        raise
                    yield _Item(key, None, failure)
                    continue
                if not isinstance(record, dict):
                    yield _Item(
                        key,
                        None,
                        RuntimeFailure(Category.MALFORMED_RESPONSE, "record is not an object"),
                    )
                    continue
                yield _Item(key, record)
            return
        for record in paginate(self._source_page, page_size=spec.page_size):
            yield _Item(None, record)

    def _source_page(self, page: int, size: int) -> tuple[list[dict[str, Any]], int | None]:
        spec = self.strategy.source
        body = self.source.request(
            "GET", spec.list_path, query={spec.page_param: page, spec.size_param: size}
        )
        return _unwrap(body, spec.items_key, spec.total_key)

    # ---- one record ----------------------------------------------------------------------

    def _sync_item(self, item: _Item, report: RunReport) -> None:
        if item.failure is not None:
            report.add(
                RecordResult(item.key, Outcome.FAILED, item.failure.category, item.failure.detail)
            )
            return
        assert item.record is not None
        key_field = self.strategy.source.key_field
        if key_field not in item.record and item.key is None:
            raise _Fatal(Category.CONTRACT_DRIFT, f"source record has no field {key_field!r}")
        raw_key = item.record.get(key_field, item.key)
        key = None if raw_key is None or raw_key == "" else str(raw_key)
        if key is None:
            report.add(
                RecordResult(
                    None, Outcome.NOT_SYNCABLE, Category.NOT_SYNCABLE, "source record has no id"
                )
            )
            return
        try:
            mapped = self.to_target(item.record)
        except FieldTransformError as error:
            if isinstance(error.__cause__, MissingFieldError):
                raise _Fatal(Category.CONTRACT_DRIFT, error.detail) from error
            report.add(
                RecordResult(key, Outcome.FAILED, Category.VALIDATION, error.detail, (error.field,))
            )
            return
        try:
            outcome, detail = self._write(mapped)
        except RuntimeFailure as failure:
            if failure.category is Category.AUTH:
                raise _Fatal(Category.AUTH, failure.detail) from failure
            self._consecutive_failures += 1
            report.add(
                RecordResult(key, Outcome.FAILED, failure.category, failure.detail, failure.fields)
            )
            if (
                failure.category in _TRANSIENT
                and self._consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT
            ):
                raise _Fatal(
                    failure.category,
                    f"{self._consecutive_failures} consecutive failures; last: {failure.detail}",
                ) from failure
            return
        self._consecutive_failures = 0
        category = Category.NOT_SYNCABLE if outcome is Outcome.NOT_SYNCABLE else None
        report.add(RecordResult(key, outcome, category, detail))

    # ---- target --------------------------------------------------------------------------

    def _write(self, mapped: dict[str, JsonScalar]) -> tuple[Outcome, str]:
        if self.strategy.target.mode is TargetMode.UPSERT:
            return self._upsert(mapped)
        return self._create_or_update(mapped)

    def _get(self, identity: object) -> dict[str, Any] | None:
        try:
            found = self.target.request("GET", _path(self.strategy.target.get_path, identity))
        except RuntimeFailure as failure:
            if failure.category is Category.NOT_FOUND:
                return None
            raise
        if not isinstance(found, dict):
            raise RuntimeFailure(Category.MALFORMED_RESPONSE, "target record is not an object")
        return found

    def _body(self, mapped: Mapping[str, JsonScalar], fields: tuple[str, ...]) -> dict[str, Any]:
        spec = self.strategy.target
        return {
            f: mapped[f]
            for f in fields
            if f in mapped and not (mapped[f] is None and f in spec.omit_if_null)
        }

    def _upsert(self, mapped: dict[str, JsonScalar]) -> tuple[Outcome, str]:
        spec = self.strategy.target
        identity = mapped.get(spec.id_field)
        if identity is None:
            return Outcome.NOT_SYNCABLE, "mapped target id is null"
        existing = self._get(identity)
        body = self._body(mapped, spec.update_fields)
        if existing is not None and _same(existing, body):
            return Outcome.UNCHANGED, ""
        self.target.request(spec.update_method, _path(spec.update_path, identity), body=body)
        return (Outcome.CREATED if existing is None else Outcome.UPDATED), ""

    def _create_or_update(self, mapped: dict[str, JsonScalar]) -> tuple[Outcome, str]:
        spec = self.strategy.target
        identity = mapped.get(spec.id_field)
        if spec.id_field in mapped:
            # the mapping supplies the target identity: it is the only way to find the record
            if identity is None:
                return Outcome.NOT_SYNCABLE, "mapped target id is null"
            existing = self._get(identity)
            if existing is None and spec.id_assigned_by_target:
                return Outcome.NOT_SYNCABLE, "target assigns ids and has no record with this id"
        else:
            value = mapped.get(spec.natural_key) if spec.natural_key else None
            if value is None:
                return Outcome.NOT_SYNCABLE, "no target identity is mapped and no natural key"
            existing = self._lookup_natural(value)
        if existing is None:
            self._create(mapped)
            return Outcome.CREATED, ""
        eid = existing.get(spec.id_field, identity)
        changes = {
            f: v
            for f, v in self._body(mapped, spec.update_fields).items()
            if f not in spec.create_only and existing.get(f) != v
        }
        if not changes:
            return Outcome.UNCHANGED, ""
        body = changes if spec.update_method == "PATCH" else self._body(mapped, spec.update_fields)
        updated = self.target.request(spec.update_method, _path(spec.update_path, eid), body=body)
        self._remember(updated if isinstance(updated, dict) else {**existing, **changes})
        return Outcome.UPDATED, ""

    def _create(self, mapped: dict[str, JsonScalar]) -> None:
        spec = self.strategy.target
        body = self._body(mapped, spec.create_fields)
        try:
            created = self.target.request("POST", spec.create_path, body=body)
        except RuntimeFailure as failure:
            if failure.category not in _AMBIGUOUS or not spec.natural_key:
                raise
            # the server may have processed the request: look before posting again
            found = self._lookup_natural(mapped.get(spec.natural_key), refresh=True)
            if found is not None:
                self._remember(found)
                return
            created = self.target.request("POST", spec.create_path, body=body)
        if isinstance(created, dict):
            self._remember(created)

    # ---- natural-key index ---------------------------------------------------------------

    def _index(self, *, refresh: bool = False) -> dict[str, list[dict[str, Any]]]:
        spec = self.strategy.target
        if self._index_cache is None or refresh:
            index: dict[str, list[dict[str, Any]]] = {}
            if spec.natural_key and spec.list_path:
                for record in paginate(self._target_page, page_size=spec.page_size):
                    value = record.get(spec.natural_key)
                    if value is not None:
                        index.setdefault(_norm(value), []).append(record)
            self._index_cache = index
        return self._index_cache

    def _target_page(self, page: int, size: int) -> tuple[list[dict[str, Any]], int | None]:
        spec = self.strategy.target
        body = self.target.request(
            "GET", spec.list_path, query={spec.page_param: page, spec.size_param: size}
        )
        return _unwrap(body, spec.items_key, spec.total_key)

    def _lookup_natural(self, value: object, *, refresh: bool = False) -> dict[str, Any] | None:
        if value is None:
            return None
        matches = self._index(refresh=refresh).get(_norm(value), [])
        if len(matches) > 1:
            raise RuntimeFailure(
                Category.CONFLICT, f"natural key matches {len(matches)} target records"
            )
        return matches[0] if matches else None

    def _remember(self, record: dict[str, Any]) -> None:
        spec = self.strategy.target
        if self._index_cache is None or not spec.natural_key:
            return
        value = record.get(spec.natural_key)
        if value is None:
            return
        bucket = self._index_cache.setdefault(_norm(value), [])
        for position, known in enumerate(bucket):
            if known.get(spec.id_field) == record.get(spec.id_field):
                bucket[position] = record
                return
        bucket.append(record)


def _same(existing: Mapping[str, Any], body: Mapping[str, Any]) -> bool:
    return all(existing.get(k) == v for k, v in body.items())


def _unwrap(
    body: Any, items_key: str, total_key: str | None
) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(body, dict) or not isinstance(body.get(items_key), list):
        raise RuntimeFailure(Category.CONTRACT_DRIFT, f"list response has no {items_key!r} array")
    items = [item for item in body[items_key] if isinstance(item, dict)]
    if len(items) != len(body[items_key]):
        raise RuntimeFailure(Category.MALFORMED_RESPONSE, "list items must be objects")
    total = body.get(total_key) if total_key else None
    return items, total if isinstance(total, int) else None
