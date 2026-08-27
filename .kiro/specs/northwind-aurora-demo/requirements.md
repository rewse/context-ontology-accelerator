# Northwind Auroraデモ環境の要件

## 目的

COAのデータソース接続、スキーマスキャン、オントロジー生成、メトリクス定義、自然言語クエリを一通り検証できるNorthwindデータベースをus-east-1に構築する。

## 機能要件

1. Aurora PostgreSQL Serverless v2クラスターを既存COA VPCのprivate subnetに作成する。
2. COAのconnector security groupからPostgreSQLポートへの接続だけを許可する。
3. データベース名を`northwind`とし、接続資格情報をSecrets Managerで自動生成する。
4. IAMデータベース認証とRDS Data APIを有効にする。
5. Northwindのテーブル、主キー、外部キー、標準データを自動投入する。
6. 乱数seedを固定した合成データを追加し、標準データを含む最終件数を顧客500件、商品100件、注文5,000件、注文明細約15,000件とする。
7. 合成データの日付範囲を3年間とし、売上、値引き、配送、在庫の検証に使える値を生成する。
8. seed処理は再実行しても行数やデータを重複させない。
9. クラスターendpoint、database名、port、Secret ARNをCloudFormation outputとして公開する。
10. Northwind由来のスキーマと標準データにライセンスと出典を同梱する。

## 運用要件

1. クラスターはscale-to-zero対応のAurora PostgreSQLバージョンを使用する。実装時にus-east-1のサポート状況を確認し、利用するバージョンをコードで固定する。
2. Serverless v2の最小容量は選択したバージョンが許容する最小値とする。最大容量は実装時にAWS既定値を確認してコードへ固定し、実測値を得た後にCloudWatchのCPUUtilizationとDatabaseConnectionsを基に調整する。
3. writerは1台とし、readerは作成しない。
4. バックアップ保持期間を7日とする。
5. 削除保護を有効にする。廃止時は削除保護を明示的に解除した後、CloudFormation削除時に最終スナップショットを保持する。
6. AWS管理キーによるストレージ暗号化、Performance Insights、PostgreSQLログのCloudWatch出力を有効にする。
7. DBインスタンスをpublicly accessibleにしない。
8. `created_by=aurora-skill`と`generation_model=gpt-5`を含むタグを付与する。

## 検証要件

1. CDKテストでServerless v2、private subnet、暗号化、バックアップ、削除保護、IAM認証、Data API、security group規則を検証する。
2. seed処理のユニットテストで再現性、参照整合性、期待行数、冪等性を検証する。
3. デプロイ後にData APIで主要テーブルの行数と代表的なjoinを確認する。
4. Secrets Managerの資格情報を使ってCOAからデータソース接続とスキャンを実行できることを確認する。
