# Repair replays

`fixed/` holds the recorded fixed-start run of 2026-10-08: `calls.jsonl` (the 15 real replies, keyed by
prompt hash; the hash contains the attempt number and the feedback) and `expected.json` (what each unit
did, attempt by attempt). A fixed-start unit's attempt 0 is not here; it is served from `../codegen/`.
`smoke_tests/test_repair_replay.py` replays the run offline through the real gate and smoke test in the
sandbox CI job, and `scripts.render_reports --check` regenerates `docs/repair-eval.md` from these files and
`../../results/repair-v0.5/fixed/`. A `fresh/` directory appears only if the fresh-start phase is run, after
its own approval. To export a run: `uv run python -m scripts.export_repair_replays --phase <phase>` from
`bench/`, then `scripts.publish_repair_results`.
