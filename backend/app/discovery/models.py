"""MORPH's internal system model: what discovery learns from an OpenAPI contract.

Everything is frozen and uses tuples so a parsed model is a stable value: parsing the same
spec twice yields byte-identical JSON.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic import Field as PydanticField


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EntityRole(StrEnum):
    RESOURCE = "RESOURCE"  # a business object, e.g. Customer
    WRAPPER = "WRAPPER"  # a pagination envelope around a resource, e.g. CustomerPage
    ERROR = "ERROR"  # only ever used in error responses


class Constraints(_Frozen):
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    default: JsonValue = None

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_defaults=True)


class Field(_Frozen):
    name: str = PydanticField(description="Property name; the last segment of path.")
    path: str = PydanticField(description="Dotted path from the entity root; arrays use '[]'.")
    json_type: str = PydanticField(
        description="string, integer, number, boolean, object, array or any (untyped)."
    )
    format: str | None = None
    nullable: bool = False
    required: bool = False
    enum_values: tuple[JsonValue, ...] = ()
    description: str | None = None
    examples: tuple[JsonValue, ...] = ()
    constraints: Constraints = PydanticField(default_factory=Constraints)
    item_type: str | None = PydanticField(
        default=None, description="Element type when json_type is array."
    )
    entity_ref: str | None = PydanticField(
        default=None, description="Name of the entity this field (or its items) refers to."
    )


class Entity(_Frozen):
    name: str
    schema_name: str = PydanticField(
        description="Component schema name, or the JSON pointer of an inline schema."
    )
    description: str | None = None
    role: EntityRole
    fields: tuple[Field, ...] = ()
    wrapped_entity: str | None = PydanticField(
        default=None, description="For WRAPPER entities: the resource they paginate."
    )


class AuthScheme(_Frozen):
    name: str
    type: str = PydanticField(description="apiKey, http, oauth2 or openIdConnect.")
    scheme: str | None = PydanticField(default=None, description="HTTP scheme, e.g. bearer.")
    location: str | None = PydanticField(default=None, description="apiKey location: header...")
    header_name: str | None = PydanticField(default=None, description="apiKey parameter name.")


class Parameter(_Frozen):
    name: str
    location: str = PydanticField(description="path, query, header or cookie.")
    required: bool = False
    json_type: str = "any"
    description: str | None = None


class ResponseRef(_Frozen):
    status: str
    entity: str | None = None


class Operation(_Frozen):
    method: str
    path: str
    operation_id: str | None = None
    summary: str | None = None
    parameters: tuple[Parameter, ...] = ()
    request_entity: str | None = None
    responses: tuple[ResponseRef, ...] = ()
    auth_scheme: str | None = None
    status_codes: tuple[str, ...] = ()


class SystemModel(_Frozen):
    name: str
    api_title: str
    api_version: str
    spec_hash: str = PydanticField(description="SHA-256 of the normalised spec.")
    auth_schemes: tuple[AuthScheme, ...] = ()
    entities: tuple[Entity, ...] = ()
    operations: tuple[Operation, ...] = ()

    def entity(self, name: str) -> Entity | None:
        return next((e for e in self.entities if e.name == name), None)
