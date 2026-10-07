# The oracle

The oracle grades a generated integration against live (mock) systems. It is hand-written, lives
only under `bench/`, and the generator never sees it: the backend never imports `morph_bench`,
never reads `bench/`, and an architecture test fails if backend source names the oracle, the
answer keys, the reference pipelines or the mocks' admin path.

```powershell
cd bench
uv run pytest oracle            # needs Docker, the sandbox and mock images, and the dev Postgres
```

Results are written to `bench/.cache/oracle-results.json`: one record per check with its
scenario, category, name, pass/fail and detail.

## How a check runs

1. The benchmark acts as the human reviewer: it stores the hand-written reference pipelines as
   `APPROVED` mapping versions (and, for S3, the recorded human decision for `segment`), then the
   normal generation service builds, gates and stores the integration. The generator sees only
   the database.
2. Fresh CRM and Support mocks start on a throwaway internal network with random credentials.
   The oracle seeds exact records through the mocks' admin interface (held-out fixtures, never
   the sample records used by prompts and generated tests).
3. The integration runs in the sandbox with the mocks as its only reachable systems.
4. The oracle reads the target's own state and the mocks' request logs through the admin
   interface. It judges from those, not from what the integration reports about itself.

## Categories

| Id | What is checked |
|---|---|
| O1 field correctness | every held-out record produces the hand-written expected target values, and the run reports the expected outcome per record |
| O2 idempotency | a second run reports no creates or updates and issues **no write request**; a changed source record updates in place; identity and count are stable |
| O3 auth | the right header kind per system (API key for CRM, Bearer for Support); wrong source or target credentials stop the run after exactly one attempt with nothing written; missing credentials stop it with no request at all |
| O4 pagination / volume | 0, 1, 100, 101 and 250 records (S3, which cannot be listed: 0, 1, 101 keys); each synced exactly once; no extra list requests |
| O5 fault injection | 500s, 429 with `Retry-After`, malformed JSON, latency, dropped fields, timeouts: ends by itself within the time limit, honours `Retry-After`, converges to the right state without duplicates, and treats a dropped field as drift rather than as null |
| O6 failure mapping | records the target rejects fail as `VALIDATION` naming the field and do not stop the others; unsyncable records are reported as such; missing source records fail as `NOT_FOUND` |
| O7 contract drift | a renamed source field stops the run with `CONTRACT_DRIFT` before anything is written; a renamed target field fails every write cleanly; drift in a field the mapping never uses raises no false alarm |
| O8 review gate | a required mapping still under review (or UNRESOLVED) produces no code and a `BLOCKED_PENDING_REVIEW` result naming the field |

## Scoring

Every check is one test id, passed or failed. Results are reported as passed/total per category
and scenario. `integration_correct` is true when no check in O1 to O7 failed. There are no
weights and no composite score. Generated tests are reported separately and never mixed in.

## Fixtures

`bench/oracle/fixtures/*.yaml` hold the held-out records and **hand-written** expected values with
the reason each record exists. They are pinned in `bench/scenarios/MANIFEST.sha256` together with
the oracle code, so any change is deliberate. A bench test checks that the hand-written reference
pipelines agree with the hand-written expectations, so the fixtures cannot silently drift from the
answer keys.
