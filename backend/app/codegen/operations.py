"""Deterministic analysis of the discovered operations: how to read the source, how to write the
target, and which target fields are actually writable.

No LLM is involved. Anything the analysis cannot support is a ``PlanError`` with a stable code,
reported to the user instead of generating code that would be wrong.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from app.discovery.models import AuthScheme, Entity, EntityRole, Field, Operation, SystemModel

PAGE_PARAMS = ("page", "page_number", "pagenumber")
SIZE_PARAMS = ("page_size", "pagesize", "per_page", "perpage", "limit", "size")
TOTAL_KEYS = ("total", "count", "total_count", "totalcount")
_ONE_PATH_PARAM = re.compile(r"^(?P<prefix>[^{}]*)/\{(?P<param>[^{}]+)\}$")


class PlanCode(StrEnum):
    NO_SOURCE_ACCESS = "NO_SOURCE_ACCESS"
    UNSUPPORTED_PAGINATION = "UNSUPPORTED_PAGINATION"
    NO_TARGET_WRITE = "NO_TARGET_WRITE"
    NO_TARGET_LOOKUP = "NO_TARGET_LOOKUP"
    NO_NATURAL_KEY = "NO_NATURAL_KEY"
    UNSUPPORTED_AUTH = "UNSUPPORTED_AUTH"
    UNKNOWN_ENTITY = "UNKNOWN_ENTITY"


class PlanError(Exception):
    def __init__(self, code: PlanCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AuthPlan:
    kind: str  # "apiKey" or "bearer"
    header: str | None = None


@dataclass(frozen=True)
class SourcePlan:
    mode: str  # "LIST" or "KEYS"
    key_field: str
    auth: AuthPlan
    list_path: str = ""
    page_param: str = "page"
    size_param: str = "page_size"
    items_key: str = "items"
    total_key: str | None = None
    get_path: str = ""  # with {id}


@dataclass(frozen=True)
class TargetPlan:
    mode: str  # "UPSERT" or "CREATE_UPDATE"
    id_field: str
    auth: AuthPlan
    get_path: str  # with {id}
    update_method: str
    update_path: str  # with {id}
    create_path: str
    create_entity: str
    update_entity: str
    create_fields: tuple[Field, ...]
    update_fields: tuple[Field, ...]
    id_assigned_by_target: bool
    natural_key: str | None
    list_path: str = ""
    page_param: str = "page"
    size_param: str = "page_size"
    items_key: str = "items"
    total_key: str | None = None

    @property
    def writable(self) -> frozenset[str]:
        return frozenset(f.name for f in (*self.create_fields, *self.update_fields))


@dataclass(frozen=True)
class OperationPlan:
    source: SourcePlan
    target: TargetPlan
    page_size: int = 100


def _entity(model: SystemModel, name: str) -> Entity:
    entity = model.entity(name)
    if entity is None:
        raise PlanError(PlanCode.UNKNOWN_ENTITY, f"{model.name} has no entity {name!r}")
    return entity


def _response_entity(operation: Operation) -> str | None:
    for response in operation.responses:
        if response.status.startswith("2") and response.entity:
            return response.entity
    return None


def _auth(model: SystemModel, operation: Operation) -> AuthPlan:
    scheme: AuthScheme | None = next(
        (s for s in model.auth_schemes if s.name == operation.auth_scheme), None
    )
    if scheme is None and len(model.auth_schemes) == 1:
        scheme = model.auth_schemes[0]
    if scheme is None:
        raise PlanError(
            PlanCode.UNSUPPORTED_AUTH, f"{model.name}: no auth scheme for the operation"
        )
    if scheme.type == "apiKey" and scheme.location == "header" and scheme.header_name:
        return AuthPlan("apiKey", scheme.header_name)
    if scheme.type == "http" and (scheme.scheme or "").lower() == "bearer":
        return AuthPlan("bearer")
    raise PlanError(
        PlanCode.UNSUPPORTED_AUTH, f"{model.name}: unsupported auth scheme {scheme.type}"
    )


def _by_id(model: SystemModel, method: str, entity: str) -> tuple[Operation, str, str] | None:
    """A ``METHOD /things/{param}`` operation whose 2xx response is ``entity``."""
    for op in model.operations:
        match = _ONE_PATH_PARAM.match(op.path)
        if op.method.upper() == method and match and _response_entity(op) == entity:
            return op, match["param"], f"{match['prefix']}/{{id}}"
    return None


def _list_op(model: SystemModel, entity: str) -> tuple[Operation, Entity] | None:
    for op in model.operations:
        if op.method.upper() != "GET" or "{" in op.path:
            continue
        wrapper = model.entity(_response_entity(op) or "")
        if wrapper and wrapper.role is EntityRole.WRAPPER and wrapper.wrapped_entity == entity:
            return op, wrapper
    return None


def _pagination(
    model: SystemModel, op: Operation, wrapper: Entity, entity: str
) -> dict[str, str | None]:
    query = {p.name.lower(): p.name for p in op.parameters if p.location == "query"}
    page = next((query[n] for n in PAGE_PARAMS if n in query), None)
    size = next((query[n] for n in SIZE_PARAMS if n in query), None)
    items = next(
        (f.name for f in wrapper.fields if f.json_type == "array" and f.entity_ref == entity), None
    )
    if page is None or size is None or items is None:
        raise PlanError(
            PlanCode.UNSUPPORTED_PAGINATION,
            f"{model.name}: {op.method} {op.path} is not page/page_size pagination",
        )
    total = next(
        (
            f.name
            for f in wrapper.fields
            if f.json_type == "integer" and f.name.lower() in TOTAL_KEYS
        ),
        None,
    )
    return {"page": page, "size": size, "items": items, "total": total}


def _source(model: SystemModel, entity_name: str) -> SourcePlan:
    entity = _entity(model, entity_name)
    names = {f.name for f in entity.fields}
    listing = _list_op(model, entity_name)
    by_id = _by_id(model, "GET", entity_name)
    if listing is None and by_id is None:
        raise PlanError(
            PlanCode.NO_SOURCE_ACCESS,
            f"{model.name} has neither a list nor a get-by-id operation for {entity_name}",
        )
    key_field = by_id[1] if by_id and by_id[1] in names else ""
    if listing is not None:
        op, wrapper = listing
        paging = _pagination(model, op, wrapper, entity_name)
        key_field = key_field or next((f.name for f in entity.fields if f.required), "")
        return SourcePlan(
            mode="LIST",
            key_field=key_field,
            auth=_auth(model, op),
            list_path=op.path,
            page_param=str(paging["page"]),
            size_param=str(paging["size"]),
            items_key=str(paging["items"]),
            total_key=paging["total"],
            get_path=by_id[2] if by_id else "",
        )
    assert by_id is not None
    if not key_field:
        raise PlanError(PlanCode.NO_SOURCE_ACCESS, f"path parameter {by_id[1]!r} is not a field")
    return SourcePlan(
        mode="KEYS", key_field=key_field, auth=_auth(model, by_id[0]), get_path=by_id[2]
    )


def _write_fields(
    model: SystemModel, name: str | None, fallback: str
) -> tuple[str, tuple[Field, ...]]:
    entity = _entity(model, name or fallback)
    return entity.name, entity.fields


def _target(model: SystemModel, entity_name: str) -> TargetPlan:
    entity = _entity(model, entity_name)
    names = {f.name for f in entity.fields}
    getter = _by_id(model, "GET", entity_name)
    if getter is None:
        raise PlanError(
            PlanCode.NO_TARGET_LOOKUP, f"{model.name} cannot fetch a {entity_name} by id"
        )
    get_op, id_param, get_path = getter
    if id_param not in names:
        raise PlanError(PlanCode.NO_TARGET_LOOKUP, f"path parameter {id_param!r} is not a field")
    put = _by_id(model, "PUT", entity_name)
    patch = _by_id(model, "PATCH", entity_name)
    create = next(
        (
            op
            for op in model.operations
            if op.method.upper() == "POST"
            and "{" not in op.path
            and _response_entity(op) == entity_name
        ),
        None,
    )
    auth = _auth(model, get_op)
    listing = _list_op(model, entity_name)
    list_args: dict[str, object] = {}
    if listing is not None:
        paging = _pagination(model, listing[0], listing[1], entity_name)
        list_args = {
            "list_path": listing[0].path,
            "page_param": paging["page"],
            "size_param": paging["size"],
            "items_key": paging["items"],
            "total_key": paging["total"],
        }
    if put is not None and put[0].request_entity == entity_name:
        _, fields = _write_fields(model, put[0].request_entity, entity_name)
        return TargetPlan(
            mode="UPSERT", id_field=id_param, auth=auth, get_path=get_path,
            update_method="PUT", update_path=put[2], create_path="",
            create_entity=entity_name, update_entity=entity_name,
            create_fields=fields, update_fields=fields, id_assigned_by_target=False,
            natural_key=None, **list_args,  # type: ignore[arg-type]
        )  # fmt: skip
    update = patch or put
    if create is None or update is None:
        raise PlanError(
            PlanCode.NO_TARGET_WRITE,
            f"{model.name} needs a PUT upsert, or a POST plus a PATCH or PUT, for {entity_name}",
        )
    create_entity, create_fields = _write_fields(model, create.request_entity, entity_name)
    update_entity, update_fields = _write_fields(model, update[0].request_entity, entity_name)
    assigned = id_param not in {f.name for f in create_fields}
    natural = None
    if assigned:
        natural = next(
            (f.name for f in create_fields if f.format == "email" or "email" in f.name.lower()),
            None,
        )
        if natural is None or not list_args:
            raise PlanError(
                PlanCode.NO_NATURAL_KEY,
                f"{model.name} assigns {entity_name} ids itself and offers no writable email-like "
                "field with a list operation to look records up by, so a sync could not be "
                "idempotent",
            )
    return TargetPlan(
        mode="CREATE_UPDATE", id_field=id_param, auth=auth, get_path=get_path,
        update_method=update[0].method.upper(), update_path=update[2],
        create_path=create.path, create_entity=create_entity, update_entity=update_entity,
        create_fields=create_fields, update_fields=update_fields,
        id_assigned_by_target=assigned, natural_key=natural, **list_args,  # type: ignore[arg-type]
    )  # fmt: skip


def analyse(
    source: SystemModel, source_entity: str, target: SystemModel, target_entity: str
) -> OperationPlan:
    """Plan the integration, or raise PlanError saying exactly what is not supported."""
    return OperationPlan(
        source=_source(source, source_entity), target=_target(target, target_entity)
    )
