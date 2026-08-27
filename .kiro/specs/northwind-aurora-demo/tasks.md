# Northwind Auroraデモ環境 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 既存COA VPC内にNorthwindデータ入りAurora PostgreSQL Serverless v2を再現可能なCDKスタックとして構築し、COAからスキャンできる状態にする。

**Architecture:** `NorthwindDemoStack`がAurora、Secret、security group、Data API seed custom resourceを所有する。seed Lambdaは標準Northwindデータを投入した後、固定seedの合成データで指定件数まで補完し、適用済みハッシュを記録して再実行時の重複を防ぐ。

**Tech Stack:** AWS CDK v2 TypeScript、Aurora PostgreSQL 17.10、Aurora Serverless v2、AWS Secrets Manager、RDS Data API、Python 3.12 Lambda、Jest、pytest

## Global Constraints

- Aurora PostgreSQL 17.10をus-east-1で使用する。
- Serverless v2は最小0 ACU、最大2 ACU、自動停止まで300秒とする。
- writerは1台とし、readerは作成しない。
- DBインスタンスをpublicly accessibleにしない。
- 既存COA VPCのprivate subnetへ配置し、connector security groupからTCP 5432だけを許可する。
- IAMデータベース認証、RDS Data API、Performance Insights、PostgreSQLログ出力を有効にする。
- AWS管理キーでストレージを暗号化する。
- バックアップを7日保持し、削除保護を有効にし、removal policyをsnapshotとする。
- SecretとDBパスワードをログ、テスト出力、CloudFormation outputへ出さない。
- 標準データを含む最終件数を顧客500件、商品100件、注文5,000件、注文明細約15,000件とする。
- 合成データは固定seed `20260827`で3年間に分散させる。
- `created_by=aurora-skill`と`generation_model=gpt-5`を付与する。

---

### Task 1: Northwindデータ資産と合成データ生成器

**Files:**
- Create: `infra/lib/lambdas/northwind-seed/assets/LICENSE`
- Create: `infra/lib/lambdas/northwind-seed/assets/base-data.sql`
- Create: `infra/lib/lambdas/northwind-seed/assets/schema.sql`
- Create: `infra/lib/lambdas/northwind-seed/generator.py`
- Test: `infra/test/lambdas/test_northwind_seed_generator.py`

**Interfaces:**
- Consumes: 固定seed `20260827`と`TargetCounts`
- Produces: `generate_top_up(seed: int, targets: TargetCounts) -> GeneratedRows`
- Produces: `BASE_COUNTS`、`DEFAULT_TARGETS`、`GeneratedRows.final_counts()`

- [ ] **Step 1: Northwind資産の出典とライセンスを固定する**

`pthom/northwind_psql`の`northwind.sql`を基に、DDLを`schema.sql`、INSERTを`base-data.sql`へ分離する。`DROP`、owner、tablespace、PostgreSQL dump session設定は除外し、PK、FK、index、sequence、標準データを残す。`assets/LICENSE`には元リポジトリのMicrosoft Public License本文、元URL、取得commit SHAを記録する。

- [ ] **Step 2: 失敗する生成器テストを書く**

```python
def test_default_targets_include_base_and_generated_rows(generator):
    rows = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    assert rows.final_counts() == {
        "customers": 500,
        "order_details": 15000,
        "orders": 5000,
        "products": 100,
    }


def test_same_seed_is_reproducible(generator):
    first = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    second = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    assert first == second


def test_every_order_detail_references_generated_or_base_keys(generator):
    rows = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    order_ids = generator.BASE_ORDER_IDS | {row["order_id"] for row in rows.orders}
    product_ids = generator.BASE_PRODUCT_IDS | {row["product_id"] for row in rows.products}
    assert all(row["order_id"] in order_ids for row in rows.order_details)
    assert all(row["product_id"] in product_ids for row in rows.order_details)
```

- [ ] **Step 3: 生成器テストが失敗することを確認する**

Run: `uv run pytest infra/test/lambdas/test_northwind_seed_generator.py -q`

Expected: FAIL with `FileNotFoundError`または`AttributeError: generate_top_up`。

- [ ] **Step 4: 最小の生成器を実装する**

```python
@dataclass(frozen=True)
class TargetCounts:
    customers: int = 500
    order_details: int = 15_000
    orders: int = 5_000
    products: int = 100


BASE_COUNTS = {
    "customers": 91,
    "order_details": 2_155,
    "orders": 830,
    "products": 77,
}
BASE_ORDER_IDS = frozenset(range(10_248, 11_078))
BASE_PRODUCT_IDS = frozenset(range(1, 78))
DEFAULT_TARGETS = TargetCounts()


@dataclass(frozen=True)
class GeneratedRows:
    customers: tuple[dict[str, object], ...]
    order_details: tuple[dict[str, object], ...]
    orders: tuple[dict[str, object], ...]
    products: tuple[dict[str, object], ...]

    def final_counts(self) -> dict[str, int]:
        return {
            "customers": BASE_COUNTS["customers"] + len(self.customers),
            "order_details": BASE_COUNTS["order_details"] + len(self.order_details),
            "orders": BASE_COUNTS["orders"] + len(self.orders),
            "products": BASE_COUNTS["products"] + len(self.products),
        }


def generate_top_up(seed: int, targets: TargetCounts) -> GeneratedRows:
    rng = random.Random(seed)
    products = tuple(_product(product_id, rng) for product_id in range(78, targets.products + 1))
    customers = tuple(_customer(index, rng) for index in range(1, targets.customers - 91 + 1))
    orders = tuple(_order(order_id, rng, customers) for order_id in range(11_078, 11_078 + targets.orders - 830))
    order_details = _order_details(rng, orders, products, targets.order_details - 2_155)
    return GeneratedRows(customers, order_details, orders, products)
```

`_customer(index, rng)`は`S0001`形式のcustomer ID、会社名、担当者名、国、都市、住所を返す。`_product(product_id, rng)`は既存category IDとsupplier IDを参照し、価格と在庫を返す。`_order(order_id, rng, customers)`はbase customerと合成customerから顧客を選び、固定基準日`2026-08-27`から過去3年間へ注文日を分散させる。`required_date`は`order_date`より後、`shipped_date`は80%を期限内、15%を期限後、5%を未発送にする。`_order_details`は各orderへ1件以上を割り当て、合計件数が目標と一致するまでproduct、quantity、unit price、discountを生成する。

- [ ] **Step 5: 生成器テストを通す**

Run: `uv run pytest infra/test/lambdas/test_northwind_seed_generator.py -q`

Expected: PASS。

- [ ] **Step 6: 生成器をコミットする**

```bash
git add infra/lib/lambdas/northwind-seed infra/test/lambdas/test_northwind_seed_generator.py
git commit -m "feat: add deterministic Northwind seed data"
```

### Task 2: Data API seed custom resource handler

**Files:**
- Create: `infra/lib/lambdas/northwind-seed/index.py`
- Test: `infra/test/lambdas/test_northwind_seed_handler.py`

**Interfaces:**
- Consumes: `generate_top_up(seed, targets)`、`ResourceProperties.ClusterArn`、`ResourceProperties.SecretArn`、`ResourceProperties.DatabaseName`、`ResourceProperties.SeedHash`
- Produces: `handler(event: dict, context: object = None) -> dict`
- Produces: `SeedConfig(cluster_arn: str, secret_arn: str, database_name: str, seed_hash: str)`
- Produces: Physical resource ID `northwind-seed-{SeedHash}`

- [ ] **Step 1: 失敗するhandlerテストを書く**

```python
def test_create_runs_schema_base_and_generated_data(handler):
    result = handler.handler(event("Create"))
    assert result["PhysicalResourceId"] == "northwind-seed-sha256-test"
    handler._rds.begin_transaction.assert_called_once()
    handler._rds.commit_transaction.assert_called_once()


def test_same_hash_skips_reseed(handler):
    handler._seed_is_current.return_value = True
    handler.handler(event("Update"))
    handler._rds.begin_transaction.assert_not_called()


def test_failure_rolls_back_and_propagates(handler):
    handler._rds.batch_execute_statement.side_effect = RuntimeError("insert failed")
    with pytest.raises(RuntimeError, match="insert failed"):
        handler.handler(event("Create"))
    handler._rds.rollback_transaction.assert_called_once()


def test_delete_is_no_op(handler):
    result = handler.handler(event("Delete"))
    assert result["PhysicalResourceId"].startswith("northwind-seed-")
    handler._rds.execute_statement.assert_not_called()


def test_transient_resume_error_is_retried(handler):
    handler._rds.execute_statement.side_effect = [
        client_error("DatabaseResumingException"),
        {"numberOfRecordsUpdated": 0},
    ]
    handler._execute(config(), "SELECT 1")
    assert handler._rds.execute_statement.call_count == 2


def test_transient_error_stops_after_five_attempts(handler):
    handler._rds.execute_statement.side_effect = client_error("DatabaseUnavailableException")
    with pytest.raises(ClientError):
        handler._execute(config(), "SELECT 1")
    assert handler._rds.execute_statement.call_count == 5
```

- [ ] **Step 2: handlerテストが失敗することを確認する**

Run: `uv run pytest infra/test/lambdas/test_northwind_seed_handler.py -q`

Expected: FAIL because `index.py` does not exist。

- [ ] **Step 3: SQL分割とData API呼び出しを実装する**

```python
@dataclass(frozen=True)
class SeedConfig:
    cluster_arn: str
    secret_arn: str
    database_name: str
    seed_hash: str


def _execute(config: SeedConfig, sql: str, *, transaction_id: str | None = None) -> dict:
    request = {
        "resourceArn": config.cluster_arn,
        "secretArn": config.secret_arn,
        "database": config.database_name,
        "sql": sql,
    }
    if transaction_id:
        request["transactionId"] = transaction_id
    return _call_with_retry(lambda: _rds.execute_statement(**request))


def _batch(config: SeedConfig, sql: str, parameter_sets: list[list[dict]], transaction_id: str) -> None:
    for chunk in _chunks(parameter_sets, 500):
        _call_with_retry(
            lambda chunk=chunk: _rds.batch_execute_statement(
                resourceArn=config.cluster_arn,
                secretArn=config.secret_arn,
                database=config.database_name,
                sql=sql,
                parameterSets=chunk,
                transactionId=transaction_id,
            )
        )


def _call_with_retry(operation: Callable[[], dict]) -> dict:
    for attempt in range(5):
        try:
            return operation()
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code not in TRANSIENT_CODES or attempt == 4:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")
```

`schema.sql`と`base-data.sql`はコメントとquoted stringを壊さずsemicolon単位へ分割する。各Data API requestは4 MiB未満に保つ。Secret値やData API parameter valuesをINFOログへ出さない。

- [ ] **Step 4: トランザクションと冪等性を実装する**

```python
def _apply_seed(config: SeedConfig) -> None:
    if _seed_is_current(config):
        return
    transaction_id = _rds.begin_transaction(
        resourceArn=config.cluster_arn,
        secretArn=config.secret_arn,
        database=config.database_name,
    )["transactionId"]
    try:
        _create_schema(config, transaction_id)
        _load_base_data(config, transaction_id)
        _load_generated_data(config, transaction_id)
        _record_seed_hash(config, transaction_id)
        _rds.commit_transaction(resourceArn=config.cluster_arn, secretArn=config.secret_arn, transactionId=transaction_id)
    except Exception:
        _rds.rollback_transaction(resourceArn=config.cluster_arn, secretArn=config.secret_arn, transactionId=transaction_id)
        raise
```

hashが変わったUpdateでは、子テーブルから依存順にtruncateしてから全データを再投入する。Deleteはデータを削除しない。`DatabaseResumingException`、`DatabaseUnavailableException`、`ServiceUnavailableError`は指数backoff付きで5回まで再試行し、その他の例外は即時に伝播させる。テストでは一時エラー後の成功と再試行上限超過を検証する。

- [ ] **Step 5: handlerテストを通す**

Run: `uv run pytest infra/test/lambdas/test_northwind_seed_handler.py -q`

Expected: PASS。

- [ ] **Step 6: handlerをコミットする**

```bash
git add infra/lib/lambdas/northwind-seed/index.py infra/test/lambdas/test_northwind_seed_handler.py
git commit -m "feat: seed Northwind through the RDS Data API"
```

### Task 3: Aurora Serverless v2 CDK stack

**Files:**
- Create: `infra/lib/stacks/services/northwind-demo-stack.ts`
- Modify: `infra/lib/stacks/services/index.ts:4-14`
- Test: `infra/test/services/northwind-demo-stack.test.ts`

**Interfaces:**
- Consumes: `NorthwindDemoStackProps.network: NetworkStack`
- Produces: `cluster: rds.DatabaseCluster`、`secret: secretsmanager.ISecret`
- Produces: CloudFormation outputs `NorthwindClusterEndpoint`、`NorthwindDatabaseName`、`NorthwindPort`、`NorthwindSecretArn`

- [ ] **Step 1: 失敗するCDKテストを書く**

```typescript
expect(cluster.DatabaseName).toBe("northwind");
expect(cluster.EngineVersion).toBe("17.10");
expect(cluster.EnableHttpEndpoint).toBe(true);
expect(cluster.EnableIAMDatabaseAuthentication).toBe(true);
expect(cluster.DeletionProtection).toBe(true);
expect(cluster.BackupRetentionPeriod).toBe(7);
expect(cluster.ServerlessV2ScalingConfiguration).toEqual({
  MinCapacity: 0,
  MaxCapacity: 2,
  SecondsUntilAutoPause: 300,
});
expect(instance.DBInstanceClass).toBe("db.serverless");
expect(instance.PubliclyAccessible).toBe(false);
```

テストでは`AWS::EC2::SecurityGroupIngress`のsourceが`network.connectorSecurityGroup`、portが5432であること、Secretが生成されること、seed Lambda timeoutが900秒であること、必要なRDS Data APIとSecret権限だけを持つことも検証する。

- [ ] **Step 2: CDKテストが失敗することを確認する**

Run: `pnpm --filter coa-infra exec jest test/services/northwind-demo-stack.test.ts --runInBand`

Expected: FAIL because `NorthwindDemoStack` is not exported。

- [ ] **Step 3: clusterとnetwork境界を実装する**

```typescript
const engine = rds.DatabaseClusterEngine.auroraPostgres({
  version: rds.AuroraPostgresEngineVersion.of("17.10", "17"),
});

this.cluster = new rds.DatabaseCluster(this, "Cluster", {
  engine,
  writer: rds.ClusterInstance.serverlessV2("Writer", {
    enablePerformanceInsights: true,
    publiclyAccessible: false,
  }),
  readers: [],
  vpc: props.network.vpc,
  vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
  credentials: rds.Credentials.fromGeneratedSecret("northwind_admin"),
  defaultDatabaseName: "northwind",
  enableDataApi: true,
  iamAuthentication: true,
  serverlessV2MinCapacity: 0,
  serverlessV2MaxCapacity: 2,
  serverlessV2AutoPauseDuration: cdk.Duration.seconds(300),
  backup: { retention: cdk.Duration.days(7) },
  deletionProtection: true,
  cloudwatchLogsExports: ["postgresql"],
  storageEncrypted: true,
  removalPolicy: cdk.RemovalPolicy.SNAPSHOT,
});
```

DB security groupを別に作り、`connections.allowFrom(props.network.connectorSecurityGroup, ec2.Port.tcp(5432))`だけを追加する。clusterとinstanceに`created_by`、`generation_model`、既存project tagを付与する。

- [ ] **Step 4: seed custom resourceをstackへ接続する**

Python 3.12 Lambdaへ`northwind-seed`ディレクトリをassetとして渡す。cluster ARN、Secret ARN、database名、schemaとgenerator設定から計算したSHA-256をcustom resource propertyへ設定する。Lambdaには`rds-data:BeginTransaction`、`BatchExecuteStatement`、`CommitTransaction`、`ExecuteStatement`、`RollbackTransaction`と対象Secretの`GetSecretValue`だけを許可する。

- [ ] **Step 5: CDKテストを通す**

Run: `pnpm --filter coa-infra exec jest test/services/northwind-demo-stack.test.ts --runInBand`

Expected: PASS。

- [ ] **Step 6: stackをコミットする**

```bash
git add infra/lib/stacks/services/northwind-demo-stack.ts infra/lib/stacks/services/index.ts infra/test/services/northwind-demo-stack.test.ts
git commit -m "feat: add Northwind Aurora demo stack"
```

### Task 4: CDK app配線と利用手順

**Files:**
- Modify: `Makefile:1-95`
- Modify: `infra/lib/constants.ts:19-29`
- Modify: `infra/bin/app.ts:9-45,78-490`
- Modify: `external-docs/content/sources.md:35-160`
- Test: `tests/unit/test_documentation_accuracy.py`

**Interfaces:**
- Consumes: CDK context `enable_northwind_demo=true`
- Produces: stack ID `coa-dev-northwind-demo`
- Produces: Make target `deploy-northwind-demo`

- [ ] **Step 1: context配線の失敗テストを追加する**

`test_documentation_accuracy.py`へ、Make targetが`enable_northwind_demo=true`を指定し、`sources.md`のコマンドと一致することを検証するtestを追加する。

```python
def test_northwind_demo_context_is_documented():
    makefile = (ROOT / "Makefile").read_text()
    docs = (ROOT / "external-docs/content/sources.md").read_text()
    assert "deploy-northwind-demo:" in makefile
    assert "--context enable_northwind_demo=true" in makefile
    assert "make deploy-northwind-demo" in docs
```

- [ ] **Step 2: testが失敗することを確認する**

Run: `uv run pytest tests/unit/test_documentation_accuracy.py -q`

Expected: FAIL on missing `deploy-northwind-demo`。

- [ ] **Step 3: context keyとapp配線を実装する**

```typescript
export const CTX_ENABLE_NORTHWIND_DEMO = "enable_northwind_demo";

const enableNorthwindDemo =
  app.node.tryGetContext(CTX_ENABLE_NORTHWIND_DEMO) === "true";
if (enableNorthwindDemo) {
  const northwind = new NorthwindDemoStack(
    app,
    `${stackPrefix}-northwind-demo`,
    { network },
  );
  northwind.addDependency(network);
}
```

通常のCDK appでは`enable_northwind_demo=true`のときだけdemo stackを合成する。`Makefile`へ次の専用targetを追加し、既存スタックを対象に含めない。

```make
deploy-northwind-demo:
	AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1 \
		pnpm --filter coa-infra exec cdk deploy coa-dev-northwind-demo \
		--context env=dev --context enable_northwind_demo=true \
		--require-approval never
```

- [ ] **Step 4: 接続手順を英語で追記する**

`sources.md`へ次の内容を追加する。

```markdown
### Optional Northwind Aurora demo

Deploy the private Aurora PostgreSQL demo database with:

`make deploy-northwind-demo`

Use the `NorthwindClusterEndpoint`, `NorthwindDatabaseName`, `NorthwindPort`, and `NorthwindSecretArn` stack outputs when registering a `JDBC_DATABASE` source with engine `POSTGRESQL`.
```

- [ ] **Step 5: 配線とドキュメントtestを通す**

Run: `uv run pytest tests/unit/test_documentation_accuracy.py -q`

Expected: PASS。

Run: `pnpm --filter coa-infra exec cdk list --context env=dev --context enable_northwind_demo=true | rg '^coa-dev-northwind-demo$'`

Expected: one matching stack ID。

- [ ] **Step 6: app配線をコミットする**

```bash
git add Makefile infra/bin/app.ts infra/lib/constants.ts external-docs/content/sources.md tests/unit/test_documentation_accuracy.py
git commit -m "feat: wire the optional Northwind demo deployment"
```

### Task 5: 全体検証とインフラレビュー

**Files:**
- Modify only if review finds a defect: files from Tasks 1 through 4
- Test: all files from Tasks 1 through 4

**Interfaces:**
- Consumes: completed CDK stack, seed handler, generator, app context
- Produces: clean synth and passing focused/full test suites

- [ ] **Step 1: Pythonのfocused testを実行する**

Run: `uv run pytest infra/test/lambdas/test_northwind_seed_generator.py infra/test/lambdas/test_northwind_seed_handler.py tests/unit/test_documentation_accuracy.py -q`

Expected: PASS。

- [ ] **Step 2: CDKのfocused testを実行する**

Run: `pnpm --filter coa-infra exec jest test/services/northwind-demo-stack.test.ts --runInBand`

Expected: PASS。

- [ ] **Step 3: format、lint、buildを実行する**

Run: `pnpm --filter coa-infra exec prettier --check "bin/**/*.ts" "lib/**/*.ts" "test/**/*.ts"`

Expected: PASS。

Run: `NX_DAEMON=false pnpm nx run infra:build`

Expected: PASS。

- [ ] **Step 4: synthとdiffを実行する**

Run: `AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1 CDK_DEFAULT_ACCOUNT=070392599442 CDK_DEFAULT_REGION=us-east-1 pnpm --filter coa-infra exec cdk synth coa-dev-northwind-demo --context env=dev --context enable_northwind_demo=true --strict`

Expected: synth succeeds without errors。

Run: `AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1 CDK_DEFAULT_ACCOUNT=070392599442 CDK_DEFAULT_REGION=us-east-1 pnpm --filter coa-infra exec cdk diff coa-dev-northwind-demo --context env=dev --context enable_northwind_demo=true --method template --no-color`

Expected: one new stack、no replacement or deletion in existing COA stacks。

- [ ] **Step 5: infrastructure code reviewを実行する**

code-reviewer agentでIAM、Secret exposure、security group、deletion protection、custom resourceのrollback、Data API statement size、seedの冪等性を確認する。指摘を修正し、focused testとsynthを再実行する。

- [ ] **Step 6: review修正をコミットする**

```bash
git add infra external-docs/content/sources.md tests/unit/test_documentation_accuracy.py
git commit -m "fix: address Northwind Aurora infrastructure review"
```

変更がなければこのcommit stepは省略する。

### Task 6: デプロイ、データ検証、COA接続

**Files:**
- No repository file changes expected

**Interfaces:**
- Consumes: stack ID `coa-dev-northwind-demo`
- Produces: deployed Aurora cluster、Secret ARN、COA source in scan-complete state

- [ ] **Step 1: 作成前確認を提示する**

ユーザーへregion、engine version、ACU範囲、自動停止、backup、deletion protection、private networking、dataset件数、付与タグを提示し、Aurora作成の明示確認を得る。

- [ ] **Step 2: stackをデプロイする**

Run: `make deploy-northwind-demo`

Expected: `coa-dev-northwind-demo` reaches `CREATE_COMPLETE`。

- [ ] **Step 3: outputと秘密情報の扱いを確認する**

Run: `aws cloudformation describe-stacks --region us-east-1 --stack-name coa-dev-northwind-demo --query 'Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}' --output table | cat`

Expected: endpoint、database name、port、Secret ARNだけが表示され、passwordは表示されない。

- [ ] **Step 4: Data APIで行数を検証する**

Secret ARNとcluster ARNをshell変数へ入れ、値自体をログへ表示せず、次のSQLを`aws rds-data execute-statement`で実行する。

```sql
SELECT 'customers' AS table_name, count(*) FROM customers
UNION ALL SELECT 'order_details', count(*) FROM order_details
UNION ALL SELECT 'orders', count(*) FROM orders
UNION ALL SELECT 'products', count(*) FROM products
ORDER BY table_name;
```

Expected: customers 500、order_details 15000、orders 5000、products 100。

- [ ] **Step 5: 代表joinを検証する**

```sql
SELECT c.company_name,
       round(sum(od.unit_price * od.quantity * (1 - od.discount))::numeric, 2) AS revenue
FROM customers c
JOIN orders o ON o.customer_id = c.customer_id
JOIN order_details od ON od.order_id = o.order_id
GROUP BY c.customer_id, c.company_name
ORDER BY revenue DESC
LIMIT 10;
```

Expected: ten rows with non-null positive revenue。

- [ ] **Step 6: COAへデータソースを登録する**

COA UIで対象namespaceを選び、source type `DATABASE`、engine `POSTGRESQL`、stack outputのendpoint、port `5432`、database `northwind`、Secret ARNを設定する。scanを開始し、statusが`PENDING_REVIEW`または`COMPLETED`になるまで確認する。

- [ ] **Step 7: COAスキャン結果を確認する**

customers、orders、order_details、products、suppliers、employeesが検出され、PK/FK関係が表示されることを確認する。system healthに新規エラーがなく、CloudFormation stack、Aurora cluster、seed custom resourceが正常であることを記録する。
