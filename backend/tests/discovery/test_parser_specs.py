"""The parser against the real mock-system contracts (v1 and the drifted v2)."""

from pathlib import Path
from typing import Any

import pytest

from app.discovery.models import EntityRole, Field, SystemModel
from app.discovery.parser import parse_spec
from app.discovery.source import load_spec

OPENAPI_DIR = Path(__file__).resolve().parents[3] / "mock_systems" / "openapi"


def parse(name: str) -> SystemModel:
    return parse_spec(load_spec(OPENAPI_DIR / f"{name}.json"), name)


@pytest.fixture(scope="module")
def crm() -> SystemModel:
    return parse("crm.v1")


@pytest.fixture(scope="module")
def support() -> SystemModel:
    return parse("support.v1")


def fields_by_name(model: SystemModel, entity: str) -> dict[str, Field]:
    found = model.entity(entity)
    assert found is not None
    return {f.name: f for f in found.fields}


# (json_type, format, nullable, required, enum_values, example, description prefix)
CUSTOMER: dict[str, tuple[Any, ...]] = {
    "customer_id": ("string", None, False, True, (), "C-1837", "Unique customer identifier"),
    "first_name": ("string", None, False, True, (), "Asha", "Given name"),
    "last_name": ("string", None, True, True, (), "Verma", "Family name"),
    "email": ("string", None, False, True, (), "asha.verma@example.com", "Primary contact"),
    "phone": ("string", None, True, True, (), "+919876543210", "Contact phone number"),
    "status": (
        "string",
        None,
        False,
        True,
        ("ACTIVE", "INACTIVE", "SUSPENDED"),
        "ACTIVE",
        "Lifecycle status",
    ),
    "segment": (
        "string",
        None,
        False,
        True,
        ("SMB", "MIDMARKET", "ENTERPRISE"),
        "MIDMARKET",
        "Commercial segment",
    ),
    "created_at": (
        "string",
        None,  # the contract publishes a plain string; the ISO-8601 rule is in the description
        False,
        True,
        (),
        "2024-03-05T10:15:30Z",
        "Creation time",
    ),
}

USER: dict[str, tuple[Any, ...]] = {
    "userId": ("integer", None, False, True, (), 1837, "Numeric user identifier"),
    "externalRef": ("string", None, True, False, (), "C-1837", "Reference to the originating"),
    "fullName": ("string", None, False, True, (), "Asha Verma", "Display name"),
    "email_address": ("string", None, False, True, (), "asha.verma@example.com", "Contact email"),
    "phoneNumber": ("string", None, True, False, (), "919876543210", "Phone number as digits"),
    "accountState": (
        "string",
        None,
        False,
        True,
        ("ENABLED", "DISABLED", "BLOCKED"),
        "ENABLED",
        "Whether the user",
    ),
    "tier": ("string", None, False, True, ("STANDARD", "PRIORITY"), "STANDARD", "Support service"),
    "createdAt": ("integer", None, False, True, (), 1709633730, "Creation time"),
}


def check_fields(actual: dict[str, Field], expected: dict[str, tuple[Any, ...]]) -> None:
    assert list(actual) == list(expected), "every field, in declaration order"
    for name, (kind, fmt, nullable, required, enum, example, prefix) in expected.items():
        f = actual[name]
        assert (f.json_type, f.format, f.nullable, f.required) == (kind, fmt, nullable, required), (
            name
        )
        assert f.enum_values == enum, name
        assert f.examples == (example,), name
        assert f.description is not None and f.description.startswith(prefix), name
        assert f.path == name


def test_crm_customer_fields(crm: SystemModel) -> None:
    check_fields(fields_by_name(crm, "Customer"), CUSTOMER)


def test_support_user_fields(support: SystemModel) -> None:
    check_fields(fields_by_name(support, "User"), USER)


def test_constraints_are_extracted(crm: SystemModel, support: SystemModel) -> None:
    customer = fields_by_name(crm, "Customer")
    assert customer["customer_id"].constraints.pattern == r"^C-\d+$"
    assert customer["first_name"].constraints.min_length == 1
    assert customer["first_name"].constraints.max_length == 100
    assert customer["phone"].constraints.pattern == r"^\+[1-9]\d{7,14}$"
    user = fields_by_name(support, "User")
    assert user["userId"].constraints.minimum == 1
    assert user["createdAt"].constraints.minimum == 0
    assert user["phoneNumber"].constraints.pattern == r"^[1-9]\d{7,14}$"


def test_create_and_patch_entities(crm: SystemModel) -> None:
    create = fields_by_name(crm, "CustomerCreate")
    assert create["last_name"].required is False and create["last_name"].nullable is True
    assert create["status"].required is False
    assert create["status"].constraints.default == "ACTIVE"
    assert create["first_name"].required is True
    patch = fields_by_name(crm, "CustomerPatch")
    assert not any(f.required for f in patch.values())
    assert patch["first_name"].nullable is False, "PATCH rejects an explicit null"
    assert patch["last_name"].nullable is True


def test_pagination_wrapper_is_detected(crm: SystemModel) -> None:
    page = crm.entity("CustomerPage")
    assert page is not None
    assert page.role is EntityRole.WRAPPER
    assert page.wrapped_entity == "Customer"
    names = [f.name for f in page.fields]
    assert names == ["items", "page", "page_size", "total"]
    items = fields_by_name(crm, "CustomerPage")["items"]
    assert (items.json_type, items.item_type, items.entity_ref) == ("array", "object", "Customer")


def test_roles(crm: SystemModel, support: SystemModel) -> None:
    crm_roles = {e.name: e.role for e in crm.entities}
    assert crm_roles["Customer"] is EntityRole.RESOURCE
    assert crm_roles["CustomerCreate"] is EntityRole.RESOURCE
    assert crm_roles["HTTPValidationError"] is EntityRole.ERROR
    assert crm_roles["ValidationError"] is EntityRole.ERROR
    support_roles = {e.name: e.role for e in support.entities}
    assert support_roles["User"] is EntityRole.RESOURCE
    for name in ("ErrorResponse", "ErrorBody", "ErrorDetail"):
        assert support_roles[name] is EntityRole.ERROR


def test_untyped_field_is_any(support: SystemModel) -> None:
    assert fields_by_name(support, "ErrorDetail")["received"].json_type == "any"


def test_scalar_union_is_kept_not_dropped(crm: SystemModel) -> None:
    loc = fields_by_name(crm, "ValidationError")["loc"]
    assert (loc.json_type, loc.item_type) == ("array", "integer|string")


def test_crm_operations_and_auth(crm: SystemModel) -> None:
    assert [(a.name, a.type, a.location, a.header_name) for a in crm.auth_schemes] == [
        ("APIKeyHeader", "apiKey", "header", "X-API-Key")
    ]
    ops = {(o.method, o.path): o for o in crm.operations}
    assert set(ops) == {
        ("GET", "/customers"),
        ("POST", "/customers"),
        ("GET", "/customers/{customer_id}"),
        ("PATCH", "/customers/{customer_id}"),
    }
    listing = ops[("GET", "/customers")]
    assert listing.auth_scheme == "APIKeyHeader"
    assert [(p.name, p.location, p.json_type, p.required) for p in listing.parameters] == [
        ("page", "query", "integer", False),
        ("page_size", "query", "integer", False),
    ]
    assert {r.status: r.entity for r in listing.responses}["200"] == "CustomerPage"
    assert {r.status: r.entity for r in listing.responses}["422"] == "HTTPValidationError"
    create = ops[("POST", "/customers")]
    assert create.request_entity == "CustomerCreate"
    assert {r.status: r.entity for r in create.responses}["201"] == "Customer"
    assert "201" in create.status_codes
    get_one = ops[("GET", "/customers/{customer_id}")]
    assert [(p.name, p.location, p.required) for p in get_one.parameters] == [
        ("customer_id", "path", True)
    ]
    assert get_one.operation_id is not None and get_one.summary == "Get a customer"


def test_support_operations_and_auth(support: SystemModel) -> None:
    assert [(a.name, a.type, a.scheme) for a in support.auth_schemes] == [
        ("HTTPBearer", "http", "bearer")
    ]
    ops = {(o.method, o.path): o for o in support.operations}
    assert set(ops) == {
        ("GET", "/users/{userId}"),
        ("POST", "/users"),
        ("PUT", "/users/{userId}"),
    }
    put = ops[("PUT", "/users/{userId}")]
    assert put.request_entity == "User"
    assert {r.status: r.entity for r in put.responses} == {
        "200": "User",
        "201": "User",
        "400": "ErrorResponse",
        "401": "ErrorResponse",
        "422": "ErrorResponse",
    }
    assert ops[("POST", "/users")].status_codes == ("201", "400", "401", "409", "422")


def test_system_metadata(crm: SystemModel) -> None:
    assert (crm.api_title, crm.api_version) == ("Mock CRM", "1.0.0")
    assert len(crm.spec_hash) == 64


def test_crm_v2_shows_renamed_field() -> None:
    v1, v2 = parse("crm.v1"), parse("crm.v2")
    v1_names = set(fields_by_name(v1, "Customer"))
    v2_names = set(fields_by_name(v2, "Customer"))
    assert v1_names - v2_names == {"phone"}
    assert v2_names - v1_names == {"phone_number"}
    assert fields_by_name(v2, "Customer")["phone_number"].constraints.pattern == (
        r"^\+[1-9]\d{7,14}$"
    )
    assert v1.spec_hash != v2.spec_hash
    assert v2.api_version == "2.0.0"


def test_support_v2_shows_renamed_field() -> None:
    v1, v2 = parse("support.v1"), parse("support.v2")
    assert set(fields_by_name(v1, "User")) - set(fields_by_name(v2, "User")) == {"tier"}
    assert set(fields_by_name(v2, "User")) - set(fields_by_name(v1, "User")) == {"serviceTier"}


def test_parsing_is_deterministic() -> None:
    first = parse("crm.v1").model_dump_json()
    second = parse("crm.v1").model_dump_json()
    assert first == second
    spec = load_spec(OPENAPI_DIR / "crm.v1.json")
    assert parse_spec(spec, "x").model_dump_json() == parse_spec(spec, "x").model_dump_json()


def test_hash_ignores_key_order_but_not_content() -> None:
    spec = load_spec(OPENAPI_DIR / "crm.v1.json")
    reordered = dict(reversed(list(spec.items())))
    assert parse_spec(reordered, "x").spec_hash == parse_spec(spec, "x").spec_hash
    changed = {**spec, "info": {**spec["info"], "version": "9.9.9"}}
    assert parse_spec(changed, "x").spec_hash != parse_spec(spec, "x").spec_hash
