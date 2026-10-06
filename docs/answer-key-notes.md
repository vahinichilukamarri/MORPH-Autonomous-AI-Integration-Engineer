# Answer-key notes: every judgement call

The answer keys in `bench/scenarios/*/answer_key.yaml` are ground truth. They are hand-written,
read only for agents and for the mapping pipeline, and protected by the checksum manifest
`bench/scenarios/MANIFEST.sha256` (see "Integrity" below). This file lists every call I made while
writing them, so you can review them before any real evaluation. Items marked **Decided** were
settled with you; the rest are mine and are the ones to challenge.

## Cross-cutting

1. **Mapping types are labels, scoring does not depend on them.** DIRECT vs TRANSFORMATION vs
   DERIVED is a judgement (is `ACTIVE -> ENABLED` a re-encoding or a different concept?). I used:
   DIRECT = copied unchanged; TRANSFORMATION = same concept, re-encoded (format, type,
   vocabulary, prefix, split); DERIVED = a different concept computed from another
   (`segment -> tier`); COMPOSITE = several fields combined. The grader reports `type_match`, but
   a mapping is *fully correct* by source set and executed transformation, not by label.
2. **Correctness is decided by examples, not by a canonical pipeline.** Each key entry is a list of
   input -> output pairs. A proposal is right when its pipeline produces every expected output.
   Different pipelines that agree on all examples are equally right, so the examples must be
   discriminating; the ones I chose include the edge cases each mapping has to survive.
3. **Every key entry is proven expressible.** `bench/references/*.yaml` holds a hand-written DSL
   pipeline for every non-UNRESOLVED entry, and a test executes each against the key's examples
   through the backend DSL. If a key entry could not be written in the DSL I would have stopped
   and told you rather than weaken the key. None failed.
4. **UNRESOLVED means flagging is the right answer.** A proposal that produces a value for an
   UNRESOLVED field counts as wrong ("guessed") however plausible it looks. An UNRESOLVED
   proposal for a field the key resolves counts as wrong too.
5. **Field coverage.** Every field of the target entity has exactly one entry. Error and wrapper
   entities are not part of any scenario.
6. **Contract values in examples are real contract values.** The loader validates every example
   input against the source field's schema and every output against the target field's schema in
   the actual OpenAPI specs, so a key cannot ask for something the target would reject.

## S1 `crm_customer_to_support_user` (unchanged since v0.1)

7. **`userId` from `customer_id`**: the numeric part as an integer; leading zeros dropped
   (`C-0042 -> 42`). The Support contract says "numeric part of the CRM id" and `int` has no
   leading zeros, so I treated `42` as the only sensible integer. An alternative reading that
   keeps zeros is impossible for an integer field.
8. **`externalRef` from `customer_id`** is DIRECT, so the same source field feeds two targets
   (`userId` and `externalRef`). This is deliberate: it is what the v0.2 retrieval baseline
   could not rank, and a mapping run has to handle one-to-many source use.
9. **`fullName`**: JOIN of `first_name` and `last_name` with one space, each part trimmed, null or
   empty parts skipped. The example `" Ravi " + "Iyer " -> "Ravi Iyer"` encodes "trimmed, single
   space" from the Support contract. A proposal that does not trim fails it.
10. **`phoneNumber`**: strip the leading `+`; null stays null. No country-code interpretation.
11. **`accountState`**: `ACTIVE -> ENABLED`, `INACTIVE -> DISABLED`, `SUSPENDED -> BLOCKED`. This
    is the natural pairing; no other pairing is defensible from the names.
12. **`tier`**: `ENTERPRISE -> PRIORITY`, `SMB` and `MIDMARKET -> STANDARD`, taken from the
    scenario requirement text (the contract itself only says "Support service tier"). It is many-to-one, so
    a validator information-loss warning is expected and is not an error.
13. **`createdAt`**: ISO-8601 UTC string to integer epoch seconds. Offsets other than `Z` are not
    exercised in the examples because the CRM always writes `Z`.

## S2 `crm_customer_to_support_user_nodocs`

14. **Same key as S1, deliberately.** Stripping documentation changes how hard the task is, not
    what is correct. The test suite asserts the two keys are identical.
15. **What "stripped" means.** A deterministic transform removes every `description`, `example`
    and `examples` from both specs. Names, types, formats, enums, patterns and bounds stay;
    schema `title`s stay (they are derived from the field names and carry no extra information).
    OpenAPI requires a description on each response, so those become empty strings. Property
    names that happen to be called `description` or `example` are not touched.
16. **Residual hints.** The constraints still say a lot (the `C-\d+` pattern on `customer_id`, the
    digits-only pattern on `phoneNumber`). I left them because removing contract constraints
    would make the specs describe different APIs, not just undocumented ones.

## S3 `support_user_to_crm_customer` (reverse direction)

17. **`customer_id`: DIRECT from `externalRef` only. Decided.** Never `C-<userId>`; users with a
    null `externalRef` are not syncable and must be flagged. The entry has `expects_review: true`:
    the grader counts a proposal as fully correct only if source and transformation are right
    *and* it is flagged (validation WARN or NEEDS_REVIEW). The null case cannot be an example
    pair because the CRM contract forbids a null id; it is described in the entry's note.
18. **`first_name` / `last_name` from `fullName`: split on the first space. Decided.** First name
    is the text before the first space; last name is the remainder, or null when there is no
    space. This inverts how Support builds `fullName`. It is wrong for multi-word first names
    (`Mary Ann Smith -> Mary / Ann Smith`), which a joined string cannot distinguish. The key
    includes that case to make the convention explicit; a proposal that splits on the last space
    (`Mary Ann / Smith`) fails it. If you would rather score this UNRESOLVED, that is a one-line
    key change plus a manifest update.
19. **`email`** is DIRECT; **`status`** is the inverse enum map (`ENABLED -> ACTIVE`,
    `DISABLED -> INACTIVE`, `BLOCKED -> SUSPENDED`), which is one-to-one here, so no information
    loss. (The forward map in S1 is also one-to-one.)
20. **`phone`**: add the `+` back; null stays null. This assumes the digits already contain the
    country code, which is how Support stores them; no country is guessed.
21. **`created_at`**: epoch seconds to ISO-8601 UTC with a trailing `Z` and no fractional
    seconds, matching what the CRM writes.
22. **`segment`: UNRESOLVED. Decided.** Support `tier` does not determine the CRM `segment`.
    Note that `PRIORITY` does imply `ENTERPRISE` in S1's forward map; I still key the whole field
    UNRESOLVED because `STANDARD` is ambiguous (SMB or MIDMARKET), segment is required and
    non-null, and a mapping that is right for half the values would silently guess for the rest.
    The grader rewards an UNRESOLVED proposal and scores any produced value as a guess.
23. **No S3 mapping for fields Support does not have.** Every CRM `Customer` field has an entry;
    `segment` is the only one without a source.

## S4 `crm_v2_to_support_v2`

24. **Same expectations as S1 against the v2 contracts.** The CRM renamed `phone` to
    `phone_number` and Support renamed `tier` to `serviceTier`. The key is S1's with those two
    names changed. The scenario's fault profile sets `contract_version: v2` so the live mocks
    agree with the contract being mapped.
25. **Sample records and prompts use the v2 field names.** The v2 sample fixtures are the v1
    ones with the same two renames, so a pipeline tied to `phone` or `tier` fails to execute.

## Integrity

26. **No manifest existed before v0.3.** I introduced `bench/scenarios/MANIFEST.sha256`, covering
    every scenario file, answer key and reference pipeline. S1's files are included unchanged
    (the S1 answer key hash is the same as in the v0.2 baseline report). `uv run python -m
    morph_bench.manifest --update` rewrites it; a test fails whenever a fixture differs from the
    manifest, so editing a key is always a deliberate two-step act.
27. **Key format additions** (S1's key does not use them): `expects_review` and `review_note` on an
    entry, `spec_transform` on a scenario, and `contract: v1|v2` on a scenario's entity
    references. The committed JSON Schemas were regenerated.
