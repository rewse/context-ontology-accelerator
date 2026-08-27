# Task 5 report: whole implementation verification

## Scope and commits

Task 5 verified the Task 1 through Task 4 implementation without deployment, push, merge, or AWS mutation. The review found two defects: Ruff rule violations in the Task 1 generator and its test, and Python bytecode included in the deployed seed Lambda asset. The fixes are committed separately.

- Fix commit: `f34ff337c05bdcedc6a311c51c8a8fce4296119e` (`fix: exclude Northwind seed bytecode assets`)
- Report commit: pending

## Verification environment

The clean worktree lacks ignored dependencies and generated Smithy assets. Verification used an ephemeral copy at `/private/tmp/northwind-aurora-task5.I6D7zy`; the source worktree and the original checkout's generated artifacts were not changed. The copy used these exact setup commands:

```bash
rsync -a --exclude .git --exclude .pytest_cache --exclude .ruff_cache --exclude .venv --exclude coverage --exclude cdk.out --exclude node_modules /Users/shibtats/git/context-ontology-accelerator/.worktrees/northwind-aurora/ /private/tmp/northwind-aurora-task5.I6D7zy/
ln -s /Users/shibtats/git/context-ontology-accelerator/node_modules /private/tmp/northwind-aurora-task5.I6D7zy/node_modules
ln -s /Users/shibtats/git/context-ontology-accelerator/infra/node_modules /private/tmp/northwind-aurora-task5.I6D7zy/infra/node_modules
ln -s /Users/shibtats/git/context-ontology-accelerator/smithy-generated /private/tmp/northwind-aurora-task5.I6D7zy/smithy-generated
```

The required root Python command still cannot initialize the project because `coa-control-plane-server` is declared as a workspace source but is not a workspace member:

```bash
uv run pytest infra/test/lambdas/test_northwind_seed_generator.py infra/test/lambdas/test_northwind_seed_handler.py tests/unit/test_documentation_accuracy.py -q
```

The exact Python 3.12 fallback command was:

```bash
uv run --no-project --python 3.12 --with pytest --with boto3 pytest infra/test/lambdas/test_northwind_seed_generator.py infra/test/lambdas/test_northwind_seed_handler.py tests/unit/test_documentation_accuracy.py -q
```

It passed with `43 passed, 1 warning`. The warning is the existing unknown `asyncio_mode` pytest configuration option in the isolated environment.

## Command results

| Command | Result |
| --- | --- |
| Focused Python fallback above | Passed: 43 tests under Python 3.12, with one existing pytest configuration warning. |
| `NODE_PATH=/private/tmp/northwind-aurora-task5.I6D7zy/infra/node_modules /private/tmp/northwind-aurora-task5.I6D7zy/infra/node_modules/.bin/jest test/services/northwind-demo-stack.test.ts --runInBand --no-coverage` | Passed: 1 suite, 6 tests. The same focused command without `--no-coverage` executed all six tests but failed the repository-wide 80% coverage threshold because the focused run counts unrelated infrastructure files. |
| `NODE_PATH=/private/tmp/northwind-aurora-task5.I6D7zy/infra/node_modules /private/tmp/northwind-aurora-task5.I6D7zy/infra/node_modules/.bin/tsc --noEmit` | Passed. |
| `NX_DAEMON=false /private/tmp/northwind-aurora-task5.I6D7zy/node_modules/.bin/nx run infra:build` | Passed. |
| `uv run --no-project --with ruff ruff check infra/lib/lambdas/northwind-seed/generator.py infra/lib/lambdas/northwind-seed/index.py infra/test/lambdas/test_northwind_seed_generator.py infra/test/lambdas/test_northwind_seed_handler.py tests/unit/test_documentation_accuracy.py` | Passed after the scoped fixes. |
| `uv run --no-project --with ruff ruff format --check infra/lib/lambdas/northwind-seed/generator.py infra/lib/lambdas/northwind-seed/index.py infra/test/lambdas/test_northwind_seed_generator.py infra/test/lambdas/test_northwind_seed_handler.py tests/unit/test_documentation_accuracy.py` | Passed: all five files formatted. |
| `/Users/shibtats/git/context-ontology-accelerator/infra/node_modules/.bin/prettier --check infra/bin/app.ts infra/lib/constants.ts infra/lib/stacks/services/index.ts infra/lib/stacks/services/northwind-demo-stack.ts infra/test/services/northwind-demo-stack.test.ts` | Passed. |
| `git diff --check` | Passed. |
| Enabled `cdk list` with `env=dev` and `enable_northwind_demo=true` | `coa-dev-northwind-demo` appeared exactly once. |
| Disabled `cdk list` with `env=dev` | `coa-dev-northwind-demo` did not appear. |
| Strict synth for `coa-dev-northwind-demo` with account `070392599442` and region `us-east-1` | Completed and produced the target template. It emitted the existing CDK cross-stack-reference warning described below. |
| `cdk diff coa-dev-northwind-demo --context env=dev --context enable_northwind_demo=true --method template --no-color` | Read-only diff reported `coa-dev-network` with no differences and one stack with differences: the new `coa-dev-northwind-demo` stack. It reported no deletion or change in existing COA stacks. |

The enabled and disabled list commands, strict synth, and diff used `AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1 CDK_DEFAULT_ACCOUNT=070392599442 CDK_DEFAULT_REGION=us-east-1` and the ephemeral copy's CDK binary. They only performed synthesis and read-only state lookup.

## Synthesized infrastructure review

- Aurora is Aurora PostgreSQL `17.10`, Serverless v2 with one private writer, 0 to 2 ACUs, a 300-second auto-pause, seven-day backup retention, encryption, IAM database authentication, the Data API, PostgreSQL logs, and Performance Insights.
- The database security group permits only TCP 5432 ingress from the imported connector security group. CDK represents `allowAllOutbound: false` with a no-op ICMP egress rule to `255.255.255.255/32`; it does not permit usable outbound database traffic.
- The cluster has both `DeletionPolicy` and `UpdateReplacePolicy` set to `Snapshot`. The Aurora writer has `Delete` for both policies because snapshots are cluster-level.
- The generated secret exposes only the `NorthwindSecretArn` output. The four outputs are `NorthwindClusterEndpoint`, `NorthwindDatabaseName`, `NorthwindPort`, and `NorthwindSecretArn`; no output contains a password.
- The seed custom resource has the hash `9b832a543690719bbf69fbc29af25a93566b427a7cbc32c0da6cbd5a5170ceda` and depends on the cluster, writer, subnet group, secret, and secret attachment.
- Seed-handler IAM is limited to RDS Data API transaction and statement actions on the cluster, `secretsmanager:GetSecretValue` on the generated secret, and write access to its own log group. Provider IAM is limited to the seed Lambda's unqualified ARN plus its own log group. No IAM policy resource contains a wildcard.
- The final seed Lambda asset contains only `index.py`, `generator.py`, `assets/LICENSE`, `assets/base-data.sql`, and `assets/schema.sql`. The asset no longer contains `__pycache__` files.

## Fixes

- Applied Ruff's required import ordering and formatting to `generator.py` and `test_northwind_seed_generator.py`. The generator behavior did not change.
- Added `exclude: ["**/__pycache__/**"]` to the seed Lambda's `Code.fromAsset` configuration. Before the fix, local Python bytecode for multiple interpreter versions was included in the Lambda artifact and could cause environment-dependent asset updates.

## Remaining risks

- The root uv workspace configuration prevents the mandated `uv run pytest` command from collecting tests. The isolated Python 3.12 fallback verifies the same focused suites without changing workspace configuration.
- Strict synth reports the existing `@aws-cdk/core:crossStackReferencesDefaultStrong` warning. The current default remains strong; resolving the warning needs a coordinated deployment of the existing stacks and is outside Task 5.
- The app reports `SSM /coa/config not found (UnknownError), using defaults` during CDK commands. Synthesis and diff completed using defaults, but the environment-specific SSM configuration was not validated here.
- The approved single generated cluster secret remains the connector interface. A separate least-privilege database user, secret, and rotation remain deferred production hardening under the Task 3 controller ruling.
- No live Aurora deployment, Data API query, or COA source scan was run. Those checks remain Task 6 work.
