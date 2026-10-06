# Discovery

Discovery turns an OpenAPI contract into MORPH's internal **system model**, stores it with
versioning, embeds every field, and retrieves the fields of another system that look most
similar. It is plain code: no LLM is called anywhere in it.

Code: `backend/app/discovery/` (model, parser, persistence) and `backend/app/embeddings/`
(providers, text templates, retrieval). API: `backend/app/api/discovery.py`.

## System model

`backend/app/discovery/models.py`, frozen Pydantic v2 models.

| Model | Content |
|---|---|
| `SystemModel` | `name`, `api_title`, `api_version`, `spec_hash`, `auth_schemes`, `entities`, `operations` |
| `Entity` | `name`, `schema_name`, `description`, `role` (`RESOURCE`, `WRAPPER`, `ERROR`), `fields`, `wrapped_entity` |
| `Field` | `name`, `path`, `json_type`, `format`, `nullable`, `required`, `enum_values`, `description`, `examples`, `constraints` (pattern, minimum, maximum, min/max length, default), `item_type`, `entity_ref` |
| `Operation` | `method`, `path`, `operation_id`, `summary`, `parameters`, `request_entity`, `responses` (status -> entity), `auth_scheme`, `status_codes` |
| `AuthScheme` | `name`, `type`, `scheme`, `location`, `header_name` |

`nullable` and `required` are independent: a field can be required yet nullable (the CRM
`Customer.last_name`), or optional and non-nullable.

## Parser

`parse_spec(spec, name)` accepts OpenAPI 3.0 and 3.1 documents, loaded from a local JSON file or
a live URL (`source.load_spec`).

1. The document is validated with `openapi-spec-validator`. Problems come back as a
   `ParseError` whose `problems` each carry a JSON pointer.
2. `$ref` is resolved fully (local references only; chains are followed). `allOf` is merged;
   `anyOf`/`oneOf` with a `null` branch, `nullable: true` and `type: [X, "null"]` all become
   `nullable`. A union of several scalar types is kept as one type written `integer|string`.
3. Fields are listed in declaration order. Inline nested objects are flattened with dotted paths
   (`address.street`); arrays of inline objects use `lines[].sku`. A property that refers to a
   named schema stays one field with `entity_ref` set instead of being copied into the parent.
4. Roles:
   - `WRAPPER`: exactly one array of a named object plus an integer paging field (`page`,
     `page_size`, `total`, ...). `wrapped_entity` points at the paginated resource.
   - `ERROR`: used only (directly or through nested references) by 4xx/5xx/`default` responses.
   - `RESOURCE`: everything else.
5. Operations carry path/query/header parameters, the request entity, the entity of every
   response status, and the first required security scheme (operation level, else global).

**Unsupported constructs are reported, never dropped.** The parser collects every problem and
raises one `ParseError`: `not`, `if/then/else`, `patternProperties`, `dependentSchemas`,
typed `additionalProperties`, unions of several non-scalar types, remote `$ref`, unresolvable or
circular `$ref`, other OpenAPI versions.

**Determinism.** The same input gives byte-identical `model_dump_json()`. Output order follows
the document. `spec_hash` is the SHA-256 of the *normalised* spec (sorted keys, no whitespace),
so key order does not change the hash, any content change does.

## Persistence and versioning

Tables (migration `0002`): `systems`, `system_versions` (version number, `spec_hash`,
`spec_json`, auth schemes), `entities`, `fields`, `operations`, `field_embeddings`
(`vector(384)`, HNSW cosine index) and `entity_embeddings`. Entities, fields and operations
belong to a *version*, not to the system.

- Ingesting a spec whose hash already exists for that system is a no-op that returns the
  existing version (`created: false`).
- A changed spec for the same system creates version N+1. Older versions keep all their rows
  and stay queryable; nothing is overwritten. The stored model can be rebuilt exactly
  (`repository.load_model`).
- The CRM contract v1 then v2 (`phone` renamed to `phone_number`) yields two versions side by
  side.

## Embeddings

`EmbeddingProvider` has two implementations, chosen by `EMBEDDING_PROVIDER`:

- `fastembed` (default): `BAAI/bge-small-en-v1.5`, 384 dimensions, run through fastembed's ONNX
  runtime; no torch. The model downloads once into `EMBEDDING_CACHE_DIR`.
- `fake`: deterministic hash-based vectors for tests. Never use it for anything reported.

The provider's `model_name` is stored with every embedding, and retrieval only ever compares
vectors of the same model.

### Text templates

Segments with no content are left out; the rest are joined with ` | `.

```
field:   <Entity>.<path> | type: <json_type> [<format>] | <description>
           | allowed values: <v1, v2, ...> | examples: <e1, e2, ...>
entity:  <Entity> | <description> | fields: <path1, path2, ...>
```

Example (CRM `Customer.status`):

```
Customer.status | type: string | Lifecycle status of the customer account. | allowed values: ACTIVE, INACTIVE, SUSPENDED | examples: ACTIVE
```

The template is covered by snapshot tests (`tests/discovery/test_embeddings.py`). Field and
entity names are embedded exactly as written in the spec.

## Retrieval

`similar_fields(session, field_id, model_name=..., k=...)` returns the k nearest fields of *other*
systems by cosine distance (pgvector), with the distance. By default candidates come from the
latest version of each other system; `target_version_id` pins a version, `target_system_id`
and `target_entity` narrow it, and `roles` filters by entity role. Asking for a field that has no
embedding under the model is an error, not an empty result.

## API (no authentication yet; Phase 2)

| Endpoint | Purpose |
|---|---|
| `POST /systems/ingest` | `{name, source: {file}` or `{url}}`; parses, stores and embeds; 422 with `problems` on an invalid spec |
| `GET /systems` | systems with their latest version |
| `GET /systems/{id}` | latest version summary: counts, auth schemes, spec hash |
| `GET /systems/{id}/versions` | every ingested version |
| `GET /systems/{id}/entities?version=N` | entities and fields (with field ids); default latest |
| `GET /fields/{id}/similar?k=5&target_system=<id>&target_entity=<name>` | nearest fields in other systems |

Ingest takes a file only from under `SPEC_ROOT` (default: the repository), and a URL is fetched
as given. Both are acceptable for local development and must be revisited before the API is
exposed beyond localhost.

## What retrieval can and cannot do

Retrieval ranks candidate fields. It does not choose between candidates, decide the mapping
type, find the transformation (`C-1837` -> `1837`) or notice that one source field feeds two
targets. Those need reasoning, which is v0.3. `docs/retrieval-baseline.md` is the measured
record of how far retrieval gets on the first scenario, and the caveats below apply to it.

- The sample is small: 8 mappings against 16 target fields (8 in `User`). The report shows the
  chance level next to every score for that reason.
- The mock contracts describe their fields generously, and some descriptions name the CRM
  directly (for example `User.userId` says it is the numeric part of the CRM customer id). That
  makes retrieval easier than it would be against a sparsely documented real API. The score is
  an upper-ish bound for this scenario and says little about harder ones; MORPH-Bench (v0.8)
  is where that gets tested.
