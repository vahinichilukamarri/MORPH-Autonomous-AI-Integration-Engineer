# Repair replays

Empty until the first real repair run exists. After a real run, `uv run python -m scripts.export_repair_replays
--phase fixed` (or `fresh`) writes `<phase>/calls.jsonl` (every real reply, keyed by prompt hash; the hash
contains the attempt number and the feedback) and `<phase>/expected.json` (what each unit did, attempt by
attempt). `smoke_tests/test_repair_replay.py` replays them offline in the sandbox CI job: a missing reply
fails the test loudly. A fixed-start unit's attempt 0 is not here; it is served from `../codegen/`.
