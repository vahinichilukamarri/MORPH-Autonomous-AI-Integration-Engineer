## Task

Propose the synchronisation strategy for copying records from the source entity to the target entity, and a few edge-case source records for testing. The field mappings are already decided and compiled; you decide only how records are read, identified, matched and written.

Write every path with `{id}` where the record identifier goes (for example `/things/{id}`), using the paths exactly as they appear in the operations below.

## How the strategy is used (trusted)

Reading the source:
- `source_mode` is `LIST` when the source has a paginated list operation (then give `source_list_path`, `source_page_param`, `source_size_param`, `source_items_key`, and `source_total_key` when the list reports a total), or `KEYS` when it can only read one record by id (then give `source_get_path`; the operator supplies the ids). Set the unused path and parameter names to null.
- `source_key_field` is the source field that identifies a record.

Writing the target:
- `target_mode` is `UPSERT` when one operation creates or replaces a record at its id (a PUT on the id path), or `CREATE_UPDATE` when records are created with POST on a collection path and changed with PATCH or PUT on the id path.
- `target_id_field` is the target field that holds the record's identity; `target_get_path` reads one record by it. `target_update_method` and `target_update_path` change an existing record; `target_create_path` creates one (null for UPSERT).
- `id_assigned_by_target` is true when the target chooses ids itself, which is the case when the identity field is not accepted in the create request. Then a record can only be updated when its id is already known.
- `target_create_fields` and `target_update_fields` are the target fields sent on create and on update. Send only fields that the matching request accepts and that have a compiled mapping.
- `create_only_fields` are sent when creating and never on update; use them for fixed values that must not overwrite an existing record. `omit_if_null_fields` are optional, non-nullable fields that are left out when the mapped value is null.
- `natural_key_field` is a field the target can be searched by when no identity is mapped (null if none); if used, give `target_list_path` and its pagination names, otherwise set them to null.

Edge records:
- `edge_record_json` holds up to 6 strings, each one JSON object that is a plausible source record (keys are source field names, values are strings, numbers, booleans or null) chosen to exercise tricky cases for the mapped fields: nulls, empty strings, extra spaces, very long or very short values, boundary numbers. Do not state what the target values should be; they are computed by software.

`rationale` is one or two short sentences.

{{SOURCE_BLOCK}}

{{TARGET_BLOCK}}

{{MAPPING_BLOCK}}
