# Task 3 report: Aurora Serverless v2 CDK stack

## Commits

- Implementation: `ea721401ad4418fad5a6f99c19349c4e4bb76c0e`

## Changed files

- `infra/lib/stacks/services/northwind-demo-stack.ts`
- `infra/lib/stacks/services/index.ts`
- `infra/test/services/northwind-demo-stack.test.ts`
- `.superpowers/sdd/northwind-aurora-demo/task-3-report.md`

## RED evidence

The CDK template test was added before the stack existed.

- `pnpm --filter coa-infra exec jest test/services/northwind-demo-stack.test.ts --runInBand` failed before Jest started. pnpm attempted an install and reported the missing ignored workspace package `@coa/control-plane-client`; `pnpm-lock.yaml` and workspace configuration were not changed.
- The shared Jest binary, run with the worktree's unmodified config, could not resolve `aws-cdk-lib` or Jest type declarations because this clean worktree has no local `infra/node_modules`. It also reported that `NorthwindDemoStack` was not exported before implementation.

## GREEN evidence

- The shared Jest binary, with `NODE_PATH` set to `/Users/shibtats/git/context-ontology-accelerator/infra/node_modules` and the worktree test root, passed `test/services/northwind-demo-stack.test.ts --runInBand`: 5 passed.
- The focused TypeScript check passed with the shared dependency directory as `baseUrl` and `typeRoots`.
- Prettier check passed for the two stack files and the focused test.
- `git diff --check` passed before the implementation commit.

## Implemented behavior

- Adds `NorthwindDemoStack` with public `cluster` and `secret` fields, a generated admin secret, and the four required CloudFormation outputs.
- Creates one private Aurora PostgreSQL 17.10 Serverless v2 writer with 0 to 2 ACUs, 300-second auto-pause, Data API, IAM authentication, Performance Insights, PostgreSQL logs, encrypted storage, seven-day backups, deletion protection, and snapshot removal.
- Restricts database ingress to TCP 5432 from the network connector security group. The seed Lambda packages the complete `northwind-seed` directory, uses Python 3.12 with a 15-minute timeout and 1024 MiB memory, and receives the four documented custom-resource properties.
- Computes a SHA-256 seed hash from the schema, base data, generator, handler, and Lambda configuration. The seed resource explicitly depends on the cluster and writer. Its role has only the five required RDS Data API actions on the cluster and `secretsmanager:GetSecretValue` on the generated secret.

## Deviation and risk

The mandated pnpm command remains blocked by the clean worktree's missing generated workspace package. The shared dependency fallback passed the focused test without modifying dependency or workspace files. No live AWS deployment was performed; deployment and Data API verification remain Task 6 work.
