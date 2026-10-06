"""Deterministic OpenAPI -> SystemModel parser. No LLM, no network (loading is in source.py).

Anything the parser cannot represent faithfully is reported as a Problem with a JSON pointer;
the parser raises one ParseError listing all of them instead of dropping data silently.
"""

from dataclasses import dataclass, field
from typing import Any

from openapi_spec_validator import OpenAPIV30SpecValidator, OpenAPIV31SpecValidator
from pydantic import JsonValue

from app.discovery.errors import ParseError, Problem
from app.discovery.models import (
    AuthScheme,
    Constraints,
    Entity,
    EntityRole,
    Operation,
    Parameter,
    ResponseRef,
    SystemModel,
)
from app.discovery.models import Field as ModelField
from app.discovery.source import spec_hash

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
COMPONENT_PREFIX = "#/components/schemas/"
PAGING_FIELDS = frozenset(
    {"page", "page_size", "per_page", "size", "total", "count", "offset", "limit", "total_pages"}
)
_UNSUPPORTED_KEYWORDS = ("not", "if", "then", "else", "patternProperties", "dependentSchemas")
_COMPOSITE_KEYS = ("allOf", "anyOf", "oneOf")


@dataclass
class _Resolved:
    schema: dict[str, Any] = field(default_factory=dict)
    name: str | None = None  # component schema this value refers to, if any
    nullable: bool = False


def parse_spec(spec: dict[str, Any], name: str) -> SystemModel:
    """Validate and parse an OpenAPI 3.0/3.1 document. Raises ParseError on any problem."""
    try:
        _validate(spec)
    except ParseError:
        raise
    except Exception as exc:  # the validator itself cannot follow the document
        # Typically an unresolvable or remote $ref. Our own walk names the JSON pointer.
        _Parser(spec, name).parse()
        raise ParseError(
            [Problem(pointer="", message=f"spec validation failed: {type(exc).__name__}: {exc}")]
        ) from exc
    return _Parser(spec, name).parse()


def _validate(spec: dict[str, Any]) -> None:
    version = str(spec.get("openapi", ""))
    if version.startswith("3.1"):
        validator: Any = OpenAPIV31SpecValidator(spec)
    elif version.startswith("3.0"):
        validator = OpenAPIV30SpecValidator(spec)
    else:
        raise ParseError(
            [Problem(pointer="/openapi", message=f"unsupported OpenAPI version {version!r}")]
        )
    problems = [
        Problem(
            pointer="/" + "/".join(_escape(str(p)) for p in error.path),
            message=str(error.message),
        )
        for error in validator.iter_errors()
    ]
    if problems:
        raise ParseError(problems)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


class _Parser:
    def __init__(self, spec: dict[str, Any], name: str) -> None:
        self.spec = spec
        self.name = name
        self.problems: list[Problem] = []
        self.inline_entities: list[Entity] = []

    def fail(self, pointer: str, message: str) -> None:
        self.problems.append(Problem(pointer=pointer, message=message))

    # ---- $ref and composition -------------------------------------------------------------

    def _lookup(self, ref: str, pointer: str) -> tuple[Any, str]:
        if not ref.startswith("#/"):
            self.fail(pointer, f"unsupported $ref {ref!r}: only local '#/...' references")
            return {}, pointer
        node: Any = self.spec
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            else:
                self.fail(pointer, f"unresolvable $ref {ref!r}")
                return {}, pointer
        return node, ref[1:]

    def resolve(self, node: Any, pointer: str, seen: tuple[str, ...] = ()) -> _Resolved:
        """Resolve $ref, allOf, anyOf/oneOf-with-null and nullable into one flat schema."""
        if not isinstance(node, dict):
            self.fail(pointer, "schema must be an object")
            return _Resolved()
        for keyword in _UNSUPPORTED_KEYWORDS:
            if keyword in node:
                self.fail(f"{pointer}/{keyword}", f"unsupported keyword '{keyword}'")
        extra = node.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            self.fail(f"{pointer}/additionalProperties", "unsupported typed additionalProperties")

        parts: list[_Resolved] = []
        name: str | None = None
        nullable = False

        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in seen:
                self.fail(pointer, f"circular $ref {ref!r}")
                return _Resolved()
            target, target_pointer = self._lookup(ref, pointer)
            resolved = self.resolve(target, target_pointer, (*seen, ref))
            parts.append(resolved)
            name = ref[len(COMPONENT_PREFIX) :] if ref.startswith(COMPONENT_PREFIX) else None
            name = name or resolved.name

        all_of = node.get("allOf")
        if isinstance(all_of, list):
            for index, item in enumerate(all_of):
                parts.append(self.resolve(item, f"{pointer}/allOf/{index}", seen))
            named = [p.name for p in parts if p.name]
            if len(parts) == 1 and named:
                name = name or named[0]

        for key in ("anyOf", "oneOf"):
            branches = node.get(key)
            if not isinstance(branches, list):
                continue
            resolved_branches = [
                self.resolve(item, f"{pointer}/{key}/{i}", seen) for i, item in enumerate(branches)
            ]
            real = [b for b in resolved_branches if b.schema.get("type") != "null"]
            if len(real) != len(resolved_branches):
                nullable = True
            if len(real) > 1:
                union = self._primitive_union(real)
                if union is None:
                    self.fail(
                        f"{pointer}/{key}", f"unsupported {key} with several non-primitive types"
                    )
                else:
                    parts.append(union)
            elif real:
                parts.append(real[0])
                name = name or real[0].name

        own = {k: v for k, v in node.items() if k not in ("$ref", *_COMPOSITE_KEYS)}
        schema_type = own.get("type")
        if isinstance(schema_type, list):
            kept = [t for t in schema_type if t != "null"]
            nullable = nullable or len(kept) != len(schema_type)
            if len(kept) > 1:
                self.fail(f"{pointer}/type", f"unsupported multi-type {schema_type!r}")
            own["type"] = kept[0] if kept else "null"
        if own.get("nullable") is True:
            nullable = True

        merged = self._merge([p.schema for p in parts] + [own])
        nullable = nullable or any(p.nullable for p in parts)
        return _Resolved(schema=merged, name=name, nullable=nullable)

    @staticmethod
    def _primitive_union(branches: list[_Resolved]) -> _Resolved | None:
        """Several scalar branches become one type written 'integer|string' (sorted)."""
        scalars = {"string", "integer", "number", "boolean"}
        types = {b.schema.get("type") for b in branches}
        simple = all(not ({"properties", "items", "enum"} & b.schema.keys()) for b in branches)
        if simple and types <= scalars:
            return _Resolved(schema={"type": "|".join(sorted(str(t) for t in types))})
        return None

    @staticmethod
    def _merge(schemas: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for schema in schemas:
            for key, value in schema.items():
                if key == "properties":
                    merged["properties"] = {**merged.get("properties", {}), **value}
                elif key == "required":
                    existing = merged.get("required", [])
                    merged["required"] = [*existing, *(v for v in value if v not in existing)]
                else:
                    merged[key] = value
        return merged

    # ---- fields ---------------------------------------------------------------------------

    @staticmethod
    def _json_type(schema: dict[str, Any]) -> str:
        declared = schema.get("type")
        if isinstance(declared, str):
            return declared
        if "properties" in schema:
            return "object"
        if "items" in schema:
            return "array"
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            first = enum[0]
            return {bool: "boolean", int: "integer", float: "number", str: "string"}.get(
                type(first), "any"
            )
        return "any"

    @staticmethod
    def _constraints(schema: dict[str, Any]) -> Constraints:
        default: JsonValue = schema.get("default")
        return Constraints(
            pattern=schema.get("pattern"),
            minimum=schema.get("minimum"),
            maximum=schema.get("maximum"),
            min_length=schema.get("minLength"),
            max_length=schema.get("maxLength"),
            default=default,
        )

    @staticmethod
    def _examples(schema: dict[str, Any]) -> tuple[JsonValue, ...]:
        examples = schema.get("examples")
        if isinstance(examples, list):
            return tuple(examples)
        if "example" in schema:
            return (schema["example"],)
        return ()

    def fields_of(self, schema: dict[str, Any], pointer: str, prefix: str = "") -> list[ModelField]:
        required = set(schema.get("required", []))
        out: list[ModelField] = []
        for prop_name, prop in schema.get("properties", {}).items():
            out.extend(
                self._field(
                    prop_name,
                    prop,
                    f"{pointer}/properties/{_escape(prop_name)}",
                    prefix,
                    prop_name in required,
                )
            )
        return out

    def _field(
        self, name: str, node: Any, pointer: str, prefix: str, required: bool
    ) -> list[ModelField]:
        resolved = self.resolve(node, pointer)
        schema = resolved.schema
        json_type = self._json_type(schema)
        path = f"{prefix}{name}"
        item_type: str | None = None
        entity_ref: str | None = None
        children: list[ModelField] = []

        if json_type == "object":
            if resolved.name:
                entity_ref = resolved.name
            else:
                children = self.fields_of(schema, pointer, f"{path}.")
        elif json_type == "array":
            items = self.resolve(schema.get("items", {}), f"{pointer}/items")
            item_type = self._json_type(items.schema)
            if item_type == "object":
                if items.name:
                    entity_ref = items.name
                else:
                    children = self.fields_of(items.schema, f"{pointer}/items", f"{path}[].")

        enum_values: tuple[JsonValue, ...] = tuple(schema.get("enum", ()))
        if json_type == "array" and not enum_values:
            inner = self.resolve(schema.get("items", {}), f"{pointer}/items")
            enum_values = tuple(inner.schema.get("enum", ()))
        own = ModelField(
            name=name,
            path=path,
            json_type=json_type,
            format=schema.get("format"),
            nullable=resolved.nullable,
            required=required,
            enum_values=enum_values,
            description=schema.get("description"),
            examples=self._examples(schema),
            constraints=self._constraints(schema),
            item_type=item_type,
            entity_ref=entity_ref,
        )
        return [own, *children]

    # ---- entities -------------------------------------------------------------------------

    def _entity(self, name: str, schema_name: str, schema: dict[str, Any], pointer: str) -> Entity:
        fields = self.fields_of(schema, pointer)
        wrapped = self._wrapped_entity(schema, pointer)
        return Entity(
            name=name,
            schema_name=schema_name,
            description=schema.get("description"),
            role=EntityRole.WRAPPER if wrapped else EntityRole.RESOURCE,
            fields=tuple(fields),
            wrapped_entity=wrapped,
        )

    def _wrapped_entity(self, schema: dict[str, Any], pointer: str) -> str | None:
        """A pagination envelope: one array of a named object plus integer paging fields."""
        collections: list[str] = []
        paging = False
        for prop_name, prop in schema.get("properties", {}).items():
            prop_pointer = f"{pointer}/properties/{_escape(prop_name)}"
            resolved = self.resolve(prop, prop_pointer)
            kind = self._json_type(resolved.schema)
            if kind == "array":
                items = self.resolve(resolved.schema.get("items", {}), f"{prop_pointer}/items")
                if self._json_type(items.schema) == "object" and items.name:
                    collections.append(items.name)
            elif kind == "integer" and prop_name in PAGING_FIELDS:
                paging = True
        return collections[0] if len(collections) == 1 and paging else None

    def _schema_entities(self) -> list[Entity]:
        entities: list[Entity] = []
        schemas = self.spec.get("components", {}).get("schemas", {})
        for schema_name, node in schemas.items():
            pointer = f"/components/schemas/{_escape(schema_name)}"
            resolved = self.resolve(node, pointer)
            if self._json_type(resolved.schema) != "object":
                continue  # enums and scalars are field types, not entities
            entities.append(self._entity(schema_name, schema_name, resolved.schema, pointer))
        return entities

    # ---- operations -----------------------------------------------------------------------

    def _entity_name_for(
        self, node: Any, pointer: str, inline_name: str, role: EntityRole
    ) -> str | None:
        resolved = self.resolve(node, pointer)
        if resolved.name:
            return resolved.name
        if self._json_type(resolved.schema) == "object" and resolved.schema.get("properties"):
            entity = self._entity(inline_name, pointer, resolved.schema, pointer)
            self.inline_entities.append(entity.model_copy(update={"role": role}))
            return inline_name
        return None

    @staticmethod
    def _json_media(content: dict[str, Any]) -> Any:
        for media_type, body in content.items():
            if "json" in media_type:
                return body.get("schema")
        return None

    def _deref_object(self, node: Any, pointer: str) -> tuple[dict[str, Any], str]:
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = str(node["$ref"])
            if ref in seen:
                self.fail(pointer, f"circular $ref {ref!r}")
                return {}, pointer
            seen.add(ref)
            node, pointer = self._lookup(ref, pointer)
        return (node if isinstance(node, dict) else {}), pointer

    def _parameters(
        self, path_item: dict[str, Any], op: dict[str, Any], pointer: str
    ) -> tuple[Parameter, ...]:
        by_key: dict[tuple[str, str], Parameter] = {}
        for source, base in ((path_item, pointer.rsplit("/", 1)[0]), (op, pointer)):
            for index, raw in enumerate(source.get("parameters", [])):
                param, ppointer = self._deref_object(raw, f"{base}/parameters/{index}")
                schema = self.resolve(param.get("schema", {}), f"{ppointer}/schema").schema
                by_key[(param.get("in", ""), param.get("name", ""))] = Parameter(
                    name=param.get("name", ""),
                    location=param.get("in", ""),
                    required=bool(param.get("required", False)),
                    json_type=self._json_type(schema),
                    description=param.get("description") or schema.get("description"),
                )
        return tuple(by_key.values())

    def _operations(self) -> list[Operation]:
        operations: list[Operation] = []
        global_security = self.spec.get("security")
        for path, path_item in self.spec.get("paths", {}).items():
            for method in HTTP_METHODS:
                op = path_item.get(method)
                if op is None:
                    continue
                pointer = f"/paths/{_escape(path)}/{method}"
                op_id = op.get("operationId")
                label = op_id or f"{method}_{path}"
                request_entity = None
                body, body_pointer = self._deref_object(
                    op.get("requestBody", {}), f"{pointer}/requestBody"
                )
                schema_node = self._json_media(body.get("content", {}))
                if schema_node is not None:
                    request_entity = self._entity_name_for(
                        schema_node,
                        f"{body_pointer}/content/schema",
                        f"{label}_request",
                        EntityRole.RESOURCE,
                    )
                responses: list[ResponseRef] = []
                for status, raw in op.get("responses", {}).items():
                    resp, resp_pointer = self._deref_object(raw, f"{pointer}/responses/{status}")
                    entity = None
                    node = self._json_media(resp.get("content", {}))
                    if node is not None:
                        failing = not str(status).startswith(("1", "2", "3"))
                        entity = self._entity_name_for(
                            node,
                            f"{resp_pointer}/content/schema",
                            f"{label}_response_{status}",
                            EntityRole.ERROR if failing else EntityRole.RESOURCE,
                        )
                    responses.append(ResponseRef(status=str(status), entity=entity))
                requirements = op.get("security", global_security)
                auth = None
                if requirements:
                    auth = next(iter(requirements[0]), None)
                operations.append(
                    Operation(
                        method=method.upper(),
                        path=path,
                        operation_id=op_id,
                        summary=op.get("summary"),
                        parameters=self._parameters(path_item, op, pointer),
                        request_entity=request_entity,
                        responses=tuple(sorted(responses, key=lambda r: r.status)),
                        auth_scheme=auth,
                        status_codes=tuple(sorted(str(s) for s in op.get("responses", {}))),
                    )
                )
        return operations

    def _auth_schemes(self) -> list[AuthScheme]:
        schemes = self.spec.get("components", {}).get("securitySchemes", {})
        return [
            AuthScheme(
                name=scheme_name,
                type=body.get("type", ""),
                scheme=body.get("scheme"),
                location=body.get("in"),
                header_name=body.get("name"),
            )
            for scheme_name, body in schemes.items()
        ]

    # ---- roles ----------------------------------------------------------------------------

    @staticmethod
    def _closure(roots: set[str], entities: dict[str, Entity]) -> set[str]:
        reached: set[str] = set()
        stack = list(roots)
        while stack:
            current = stack.pop()
            entity = entities.get(current)
            if current in reached or entity is None:
                continue
            reached.add(current)
            stack.extend(f.entity_ref for f in entity.fields if f.entity_ref)
            if entity.wrapped_entity:
                stack.append(entity.wrapped_entity)
        return reached

    def _assign_roles(self, entities: list[Entity], operations: list[Operation]) -> list[Entity]:
        by_name = {e.name: e for e in entities}
        ok_roots: set[str] = set()
        error_roots: set[str] = set()
        for op in operations:
            if op.request_entity:
                ok_roots.add(op.request_entity)
            for response in op.responses:
                if response.entity is None:
                    continue
                healthy = response.status[:1] in {"1", "2", "3"}
                (ok_roots if healthy else error_roots).add(response.entity)
        ok = self._closure(ok_roots, by_name)
        errors = self._closure(error_roots, by_name) - ok
        return [
            e.model_copy(update={"role": EntityRole.ERROR}) if e.name in errors else e
            for e in entities
        ]

    # ---- entry point ----------------------------------------------------------------------

    def parse(self) -> SystemModel:
        entities = self._schema_entities()
        operations = self._operations()
        entities = self._assign_roles([*entities, *self.inline_entities], operations)
        if self.problems:
            unique = list(dict.fromkeys((p.pointer, p.message) for p in self.problems))
            raise ParseError([Problem(pointer=ptr, message=msg) for ptr, msg in unique])
        info = self.spec.get("info", {})
        return SystemModel(
            name=self.name,
            api_title=info.get("title", ""),
            api_version=str(info.get("version", "")),
            spec_hash=spec_hash(self.spec),
            auth_schemes=tuple(self._auth_schemes()),
            entities=tuple(entities),
            operations=tuple(operations),
        )
