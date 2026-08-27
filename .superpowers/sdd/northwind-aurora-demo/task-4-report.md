# Task 4 report: CDK app wiring and usage documentation

## Commits

- Implementation: `b02f3b1`

## Changed files

- `Makefile`
- `external-docs/content/sources.md`
- `infra/bin/app.ts`
- `infra/lib/constants.ts`
- `tests/unit/test_documentation_accuracy.py`
- `.superpowers/sdd/northwind-aurora-demo/task-4-report.md`

## RED evidence

The new documentation-accuracy test was added before the deployment target, context constant, app wiring, and usage guidance existed. The isolated test command failed with `IndexError` while looking up the missing `deploy-northwind-demo:` target.

The mandated `uv run pytest tests/unit/test_documentation_accuracy.py -q` command could not collect tests because the worktree's `packages/control-plane` declares `coa-control-plane-server` as a workspace dependency, but that package is absent from the workspace. The configuration and lockfile were left unchanged.

## GREEN evidence

- `uv run --no-project --with pytest pytest tests/unit/test_documentation_accuracy.py -q` passed: 7 passed. Pytest emitted one existing `asyncio_mode` configuration warning.
- `/Users/shibtats/git/context-ontology-accelerator/infra/node_modules/.bin/prettier --check infra/bin/app.ts infra/lib/constants.ts` passed.
- `git diff --check` passed before the implementation commit.
- CDK synthesis used existing local dependencies and a temporary copy that included the existing generated OpenAPI assets. It did not write generated files to the worktree. With `enable_northwind_demo=true`, `cdk list` contained `coa-dev-northwind-demo` exactly once. With the context omitted, `cdk list` did not contain it.

## Implemented behavior

- Adds the alphabetized `CTX_ENABLE_NORTHWIND_DEMO = "enable_northwind_demo"` constant.
- Adds `NorthwindDemoStack` only when the context value is exactly the string `"true"`, passes the existing `network`, and declares the network dependency.
- Adds `deploy-northwind-demo`, which deploys only `coa-dev-northwind-demo` in `us-east-1` with the required development context and no approval prompt.
- Documents the optional private Aurora demo under JDBC database guidance, including the deployment command and CloudFormation-output mapping for a `DATABASE` source using `POSTGRESQL`.
- Adds a documentation-accuracy test that keeps the Make target, CDK context and stack ID, output mapping, and private-network requirement aligned.

## Risks and follow-up

The clean worktree lacks the generated OpenAPI assets and has missing workspace packages, so ordinary `pnpm` and `uv run` validation cannot start without the documented fallback. No dependency files or generated workspace files were changed. No AWS deployment was performed; live database and source-registration verification remain Task 6 work.
