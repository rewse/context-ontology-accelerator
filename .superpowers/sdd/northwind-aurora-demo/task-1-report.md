# Task 1 report: Northwind data assets and deterministic generator

## Implementation commit

`a00550a098f859b1f1af9121c87a0ffd78a6f1e7`: `feat: add deterministic Northwind seed data`

## Changed files

- `infra/lib/lambdas/northwind-seed/assets/LICENSE`
- `infra/lib/lambdas/northwind-seed/assets/base-data.sql`
- `infra/lib/lambdas/northwind-seed/assets/schema.sql`
- `infra/lib/lambdas/northwind-seed/generator.py`
- `infra/test/lambdas/test_northwind_seed_generator.py`
- `.superpowers/sdd/northwind-aurora-demo/task-1-report.md`

## RED evidence

The generator test was added before `generator.py`. The required command was run from the repository root:

```text
uv run pytest infra/test/lambdas/test_northwind_seed_generator.py -q
```

It failed before collection because the existing root uv workspace configuration declares `coa-control-plane-server` as a workspace source but not as a workspace member. The relevant error was:

```text
Failed to parse entry: `coa-control-plane-server`
`coa-control-plane-server` references a workspace in `tool.uv.sources` ... but is not a workspace member
```

Therefore the expected missing-generator `FileNotFoundError` could not be observed through the mandated command. No workspace configuration was changed because it is outside this task's ownership.

## GREEN evidence

The focused tests passed under the Lambda target runtime version from the repository root:

```text
uvx --python 3.12 --from pytest --with boto3 pytest infra/test/lambdas/test_northwind_seed_generator.py -q
5 passed, 1 warning in 0.69s
```

The warning is the repository-level pytest setting `asyncio_mode`, which is not provided by this isolated pytest environment. It does not affect the synchronous generator tests.

Additional checks passed:

- `python3 -m py_compile infra/lib/lambdas/northwind-seed/generator.py`
- Standard data counts: customers=91, products=77, orders=830, order_details=2155.
- Schema contains 14 primary keys and 13 foreign keys, with dump settings, destructive setup, ownership, and tablespace metadata removed.

## Deviations and risks

- The mandated `uv run pytest` remains blocked by the pre-existing root workspace definition; the isolated Python 3.12 invocation is the GREEN verification workaround.
- Upstream commit `cd0ef28d66369fbe177778e604e4be0f153c9e5c` contains no explicit sequences or indexes. All supplied table definitions, primary keys, foreign keys, and standard INSERT data are retained after split.
