# Agent rules (megaspeed)

Follow these on every task in this repo.

1. **Every feature ships with automated tests.** No functionality merges without tests covering it.
   Run `.venv/bin/python -m pytest tests -q -n 8 --dist loadfile` (and `scrape/tests`) and keep
   them green. On a box without the dev deps, `tests/run_parallel.py -j 8` runs the same suite
   with the stdlib; `unittest discover -s tests` stays the reference for one failing test.
   Fixing a bug adds a regression test that fails without the fix.
2. **Pseudonymisation contract (prd/02) is non-negotiable.** No raw ticket/commit/diff/comment text
   in the store or any API response; `--verify` must pass.
3. **Read the design docs** in `prd/` before implementing; follow the closed-vocabulary event model
   and the design language (prd/10).
4. **Scoped commits.** `git add <paths>` only, never `git add -A`. Do not push to origin unless asked
   (the orchestrator pushes).
