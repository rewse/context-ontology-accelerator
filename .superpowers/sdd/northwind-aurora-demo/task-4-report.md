# Task 4 report: CDK app wiring and usage documentation

## Commits

- Implementation: `b02f3b1`
- Review fix round 1: `3eb2f7b`
- Review fix round 2: `96dd858`

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

## Review fix round 1

- The Make target test now matches a non-indented `deploy-northwind-demo:` line and consumes only following recipe lines, so it stops before the next target.
- The documentation test extracts the optional Northwind section and compares the complete ordered CloudFormation-output to source-field table pairs.
- A pytest CDK regression test copies the source into a temporary workspace, links the existing generated assets there, synthesizes enabled and default contexts, and counts exact stack-ID lines from `cdk list`. It expects one `coa-dev-northwind-demo` entry when `enable_northwind_demo=true` and zero by default.
- The CDK subprocess uses disposable output directories, disables pagers and metadata credentials, and sends the app's SSM lookup to a loopback endpoint. It performs no AWS writes and does not modify the source worktree or generated assets.

## Review fix round 1 evidence

- `uv run --no-project --with pytest pytest tests/unit/test_documentation_accuracy.py -q` passed: 8 passed. Pytest emitted one existing `asyncio_mode` configuration warning.
- `make -n deploy-northwind-demo` printed only the expected single-stack deployment command.
- `/Users/shibtats/git/context-ontology-accelerator/infra/node_modules/.bin/prettier --check infra/bin/app.ts infra/lib/constants.ts` passed.
- `git diff --check` passed before the review-fix commit.

## Review fix round 2

- Removes the parent-checkout lookup, generated-asset symlink, temporary workspace, `NODE_PATH`, subprocess, and CDK list test from the documentation-accuracy suite.
- Keeps the test static and self-contained. It now requires exactly one occurrence each of the strict context check, `NorthwindDemoStack` construction, stack ID, and network dependency.
- Retains the target-anchored Make recipe extraction and the ordered documentation table-pair assertion. Enabled and disabled `cdk list` verification remains command-level evidence for Task 4 and Task 5.

## Review fix round 2 evidence

- `uv run --no-project --with pytest pytest tests/unit/test_documentation_accuracy.py -q` passed: 7 passed. Pytest emitted one existing `asyncio_mode` configuration warning.
- `uv run --no-project --with ruff ruff format --check tests/unit/test_documentation_accuracy.py` passed.
- `uv run --no-project --with ruff ruff check tests/unit/test_documentation_accuracy.py` passed.
- `make -n deploy-northwind-demo` printed only the expected single-stack deployment command.
- `git diff --check` passed before the review-fix commit.
