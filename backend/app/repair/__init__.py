"""The repair loop around code generation: feedback, guards and the runtime smoke test.

Nothing here may read grading data. The oracle, the answer keys and the bench fixtures are
eval-only; ``tests/repair/test_isolation.py`` enforces that over the import graph and the source.
"""
