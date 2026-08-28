# Northwind Aurora CloudShell管理接続 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CloudShell VPC environmentから`shibtats`管理ユーザーでprivate Northwind Auroraへ`psql`接続できる状態を作る。

**Architecture:** 既存COA VPCのprivate subnetとconnector security groupをCloudShell VPC environmentへ割り当てる。DB roleはRDS Data APIのtransaction内で作成し、パスワードを出力せず`rds_superuser`を付与する。

**Tech Stack:** AWS CloudShell VPC environment、Aurora PostgreSQL 17.10、RDS Data API、PostgreSQL 16 client

## Global Constraints

- Auroraのpublic accessを有効にしない。
- EC2 bastion、VPC peering、新規security group ruleを作成しない。
- CloudShell environmentはus-east-1、VPC `vpc-07472b9ac55f618a4`、private subnet `subnet-0973f5eab122eec14`、connector security group `sg-01c74e78e76ce1804`を使用する。
- DB endpointは`coa-dev-northwind-demo-clustereb0386a7-v1lkqzms5xko.cluster-cqiexidavetg.us-east-1.rds.amazonaws.com`、portは5432、databaseは`northwind`とする。
- DB userは`shibtats`とし、`rds_superuser`、`CREATEDB`、`CREATEROLE`を付与する。
- パスワードは`/tmp/password.txt`から読み込み、command line、標準出力、ログ、CloudFormation output、`.pgpass`へ書き出さない。

---

### Task 1: `shibtats`管理roleの作成

**Files:**
- No repository file changes expected

**Interfaces:**
- Consumes: `/tmp/password.txt`、Northwind cluster ARN、master Secret ARN
- Produces: PostgreSQL login role `shibtats` with `rds_superuser`, `CREATEDB`, and `CREATEROLE`

- [ ] **Step 1: パスワードファイルと対象リソースを検証する**

Run:

```bash
test "$(stat -f '%Lp' /tmp/password.txt)" = "600"
test -s /tmp/password.txt
aws cloudformation describe-stacks \
  --stack-name coa-dev-northwind-demo \
  --region us-east-1 \
  --query 'Stacks[0].StackStatus' \
  --output text \
  --no-cli-pager
```

Expected: password fileはmode 600かつ非空、stack statusは`CREATE_COMPLETE`。

- [ ] **Step 2: transactionを開始してpasswordをtransaction-local settingへ設定する**

shell変数へcluster ARN、Secret ARN、transaction IDを格納し、値を表示しない。passwordはData API parameterとして渡し、SQL結果はbooleanだけを返す。

```bash
NORTHWIND_CLUSTER_ARN=$(aws rds describe-db-clusters \
  --region us-east-1 \
  --query 'DBClusters[?starts_with(DBClusterIdentifier, `coa-dev-northwind-demo-cluster`)] | [0].DBClusterArn' \
  --output text \
  --no-cli-pager)
NORTHWIND_SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name coa-dev-northwind-demo \
  --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`NorthwindSecretArn`].OutputValue | [0]' \
  --output text \
  --no-cli-pager)
NORTHWIND_TRANSACTION_ID=$(aws rds-data begin-transaction \
  --resource-arn "$NORTHWIND_CLUSTER_ARN" \
  --secret-arn "$NORTHWIND_SECRET_ARN" \
  --database northwind \
  --region us-east-1 \
  --query transactionId \
  --output text \
  --no-cli-pager)
jq -Rcn '[{name:"password",value:{stringValue:input}}]' < /tmp/password.txt | \
aws rds-data execute-statement \
  --resource-arn "$NORTHWIND_CLUSTER_ARN" \
  --secret-arn "$NORTHWIND_SECRET_ARN" \
  --database northwind \
  --transaction-id "$NORTHWIND_TRANSACTION_ID" \
  --sql "SELECT set_config('northwind.bootstrap_password', :password, true) IS NOT NULL" \
  --parameters file:///dev/stdin \
  --region us-east-1 \
  --query 'records[0][0].booleanValue' \
  --output text \
  --no-cli-pager
```

Expected: `True`だけを表示し、passwordは表示しない。

- [ ] **Step 3: roleを冪等に作成して管理権限を付与する**

次のSQLをshell変数へ格納し、`NORTHWIND_TRANSACTION_ID`内で順に実行する。

```bash
NORTHWIND_ROLE_SQL=$(cat <<'SQL'
DO $do$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shibtats') THEN
    EXECUTE format(
      'ALTER ROLE %I WITH LOGIN CREATEDB CREATEROLE PASSWORD %L',
      'shibtats',
      current_setting('northwind.bootstrap_password')
    );
  ELSE
    EXECUTE format(
      'CREATE ROLE %I WITH LOGIN CREATEDB CREATEROLE PASSWORD %L',
      'shibtats',
      current_setting('northwind.bootstrap_password')
    );
  END IF;
END
$do$;
SQL
)

aws rds-data execute-statement \
  --resource-arn "$NORTHWIND_CLUSTER_ARN" \
  --secret-arn "$NORTHWIND_SECRET_ARN" \
  --database northwind \
  --transaction-id "$NORTHWIND_TRANSACTION_ID" \
  --sql "$NORTHWIND_ROLE_SQL" \
  --region us-east-1 \
  --no-cli-pager
```

```bash
aws rds-data execute-statement \
  --resource-arn "$NORTHWIND_CLUSTER_ARN" \
  --secret-arn "$NORTHWIND_SECRET_ARN" \
  --database northwind \
  --transaction-id "$NORTHWIND_TRANSACTION_ID" \
  --sql "GRANT rds_superuser TO shibtats" \
  --region us-east-1 \
  --no-cli-pager
```

いずれかが失敗した場合は次を実行する。

```bash
aws rds-data rollback-transaction \
  --resource-arn "$NORTHWIND_CLUSTER_ARN" \
  --secret-arn "$NORTHWIND_SECRET_ARN" \
  --transaction-id "$NORTHWIND_TRANSACTION_ID" \
  --region us-east-1 \
  --no-cli-pager
unset NORTHWIND_ROLE_SQL NORTHWIND_TRANSACTION_ID
```

- [ ] **Step 4: role属性を検証してcommitする**

Run within the transaction:

```bash
aws rds-data execute-statement \
  --resource-arn "$NORTHWIND_CLUSTER_ARN" \
  --secret-arn "$NORTHWIND_SECRET_ARN" \
  --database northwind \
  --transaction-id "$NORTHWIND_TRANSACTION_ID" \
  --sql "SELECT rolname, rolcanlogin, rolcreatedb, rolcreaterole, pg_has_role('shibtats', 'rds_superuser', 'member') AS is_rds_superuser FROM pg_roles WHERE rolname = 'shibtats'" \
  --format-records-as JSON \
  --region us-east-1 \
  --query formattedRecords \
  --output text \
  --no-cli-pager
```

Expected: one row、`rolcanlogin=true`、`rolcreatedb=true`、`rolcreaterole=true`、`is_rds_superuser=true`。

Run:

```bash
aws rds-data commit-transaction \
  --resource-arn "$NORTHWIND_CLUSTER_ARN" \
  --secret-arn "$NORTHWIND_SECRET_ARN" \
  --transaction-id "$NORTHWIND_TRANSACTION_ID" \
  --region us-east-1 \
  --no-cli-pager
unset NORTHWIND_ROLE_SQL NORTHWIND_TRANSACTION_ID
```

### Task 2: CloudShell VPC environmentの作成

**Files:**
- No repository file changes expected

**Interfaces:**
- Consumes: COA VPC、private subnet、connector security group
- Produces: CloudShell VPC environment `coa-northwind-admin`

- [ ] **Step 1: AWS ConsoleでCloudShellをus-east-1へ切り替える**

AWS Consoleのregionを`us-east-1`へ設定し、CloudShellを開く。CloudShellの`Actions`から`Create VPC environment`を選択する。

- [ ] **Step 2: VPC environment設定を入力する**

次の値を設定する。

```text
Name: coa-northwind-admin
VPC: vpc-07472b9ac55f618a4
Subnet: subnet-0973f5eab122eec14
Security group: sg-01c74e78e76ce1804
```

Createを実行し、CloudShell promptが新environmentで利用可能になるまで待つ。

- [ ] **Step 3: PostgreSQL clientを導入する**

Run in CloudShell:

```bash
sudo dnf install postgresql16 -y
psql --version
```

Expected: PostgreSQL client version 16.x。

### Task 3: `psql`接続検証

**Files:**
- No repository file changes expected

**Interfaces:**
- Consumes: CloudShell environment `coa-northwind-admin`、DB user `shibtats`
- Produces: successful interactive `psql` session to database `northwind`

- [ ] **Step 1: interactive password promptで接続する**

Run in CloudShell:

```bash
psql \
  --host=coa-dev-northwind-demo-clustereb0386a7-v1lkqzms5xko.cluster-cqiexidavetg.us-east-1.rds.amazonaws.com \
  --port=5432 \
  --username=shibtats \
  --dbname=northwind \
  --password
```

password promptへ`/tmp/password.txt`の値を手動入力する。shell変数、history、`.pgpass`には保存しない。

- [ ] **Step 2: identityと主要テーブルを検証する**

Run in `psql`:

```sql
SELECT current_user, current_database();
SELECT count(*) FROM customers;
SELECT count(*) FROM orders;
SELECT pg_has_role(current_user, 'rds_superuser', 'member') AS is_rds_superuser;
```

Expected: `current_user=shibtats`、`current_database=northwind`、customers=500、orders=5000、`is_rds_superuser=true`。

- [ ] **Step 3: 接続終了後にsecret非永続化を確認する**

Run:

```bash
\q
test ! -f ~/.pgpass
history | tail -20 | python3 -c 'import sys; from pathlib import Path; password = Path("/tmp/password.txt").read_text().strip(); raise SystemExit(bool(password and password in sys.stdin.read()))'
```

Expected: `.pgpass`が存在せず、recent shell historyにpasswordが含まれない。
