# Task 2 report: RDS Data API seed handler

## Commits

- Implementation: `038731ee7a9293dd35ca4c8a1919b63b8f825a5d`

## Changed files

- `infra/lib/lambdas/northwind-seed/index.py`
- `infra/test/lambdas/test_northwind_seed_handler.py`
- `.superpowers/sdd/northwind-aurora-demo/task-2-report.md`

## RED evidence

The handler test was written before `index.py` existed.

- `uv run pytest infra/test/lambdas/test_northwind_seed_handler.py -q` could not collect tests because `coa-control-plane-server` is referenced as a uv workspace source but is not a workspace member.
- `uvx --python 3.12 --with pytest --with boto3 pytest infra/test/lambdas/test_northwind_seed_handler.py -q` reported 19 setup errors with `FileNotFoundError` for `infra/lib/lambdas/northwind-seed/index.py`.

## GREEN evidence

- `uvx --python 3.12 --with pytest --with boto3 pytest infra/test/lambdas/test_northwind_seed_handler.py -q`: 19 passed.
- `uvx --python 3.12 --with pytest --with boto3 pytest infra/test/lambdas/test_northwind_seed_generator.py -q`: 13 passed.
- `python3.12 -m py_compile infra/lib/lambdas/northwind-seed/index.py infra/test/lambdas/test_northwind_seed_handler.py`: passed.
- `git diff --check`: passed before the implementation commit.

## Implemented behavior

- Create runs schema SQL, standard data, generated data, and seed metadata in one RDS Data API transaction.
- Update skips work when the stored hash matches. A changed hash deletes only generated rows in child-first dependency order before loading the generated rows again.
- Delete returns the physical resource ID without touching the database.
- SQL splitting preserves semicolons in quoted strings, dollar-quoted strings, and comments. Data API batches stay below 500 parameter sets and 4 MiB JSON request size.
- Only `DatabaseResumingException`, `DatabaseUnavailableException`, and `ServiceUnavailableError` retry. Retries use exponential backoff for five total attempts, with injectable sleep in tests.

## Deviation and risk

The mandated root `uv run` command remains blocked before test collection by the existing incomplete workspace membership. Workspace configuration was not changed. The isolated Python 3.12 command provides the verification fallback requested by the task. The tests mock the RDS Data API; a live Aurora deployment check remains part of Task 6.
