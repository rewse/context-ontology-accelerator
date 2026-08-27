# Task 2 report: RDS Data API seed handler

## Commits

- Implementation: `038731ee7a9293dd35ca4c8a1919b63b8f825a5d`
- Review fix round 1: `ab44bce1ecac3039380fd6703cdbb2d0ffa3e874`
- Review fix round 2: `fbf04fae3f261c9227f5418b3b6252137b012215`

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
- `uvx --python 3.12 --with pytest --with boto3 pytest infra/test/lambdas/test_northwind_seed_handler.py infra/test/lambdas/test_northwind_seed_generator.py -q`: 35 passed.
- `uvx ruff check` and `uvx ruff format --check` on the Task 2 Python files: passed.
- `uvx --python 3.12 --with pytest --with boto3 pytest infra/test/lambdas/test_northwind_seed_handler.py infra/test/lambdas/test_northwind_seed_generator.py -q`: 36 passed.

## Implemented behavior

- Create runs schema SQL, standard data, generated data, and seed metadata in one RDS Data API transaction.
- Update skips work when the stored hash matches. A changed hash drops every table defined by `schema.sql` and `seed_metadata` with one `DROP TABLE IF EXISTS` statement using PostgreSQL's default RESTRICT behavior, then reruns schema SQL, standard data, generated data, and seed metadata in the same transaction.
- Delete returns the physical resource ID without touching the database.
- SQL splitting preserves semicolons in quoted strings, dollar-quoted strings, and comments. Data API batches stay below 500 parameter sets and 4 MiB JSON request size.
- Only `DatabaseResumingException`, `DatabaseUnavailableException`, and `ServiceUnavailableError` retry. Retries use exponential backoff for five total attempts, with injectable sleep in tests.

## Review fix round 1 evidence

The new tests first failed because changed-hash handling did not call the reset path. The final test set checks the actual `schema.sql` table list, includes `seed_metadata`, and verifies `SANTG` remains outside any selective-customer cleanup. It also verifies that a changed hash runs `schema.sql`, `base-data.sql`, generated rows, and metadata after the reset, and that a failure after the reset rolls back without a commit.

## Review fix round 2 evidence

The reset statement now has exact coverage of the tables declared in `schema.sql` plus `seed_metadata`, with no `CASCADE` clause. The test suite simulates an external dependency by failing the reset call. It verifies that the handler does not begin schema creation, rolls back the transaction, and does not commit.

## Deviation and risk

The mandated root `uv run` command remains blocked before test collection by the existing incomplete workspace membership. Workspace configuration was not changed. The isolated Python 3.12 command provides the verification fallback requested by the task. The tests mock the RDS Data API; a live Aurora deployment check remains part of Task 6.
