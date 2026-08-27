# Northwind Aurora CloudShell管理接続の設計

## 方針

Auroraをpublic化せず、AWS CloudShell VPC environmentから`psql`で接続する。CloudShell environmentはus-east-1のCOA VPC、private subnet、既存connector security groupを使用する。Aurora security groupはconnector security groupからTCP 5432を許可済みであるため、追加のnetwork rule、EC2 bastion、VPC peeringは作成しない。

## データベースユーザー

PostgreSQL login role `shibtats`を作成し、`northwind`データベースの管理者権限を付与する。パスワードはローカルの`/tmp/password.txt`から読み込み、画面、ログ、shell履歴、CloudFormation outputへ出力しない。既にroleが存在する場合はパスワードと権限を期待状態へ更新する。

## 接続方法

CloudShell VPC environmentへPostgreSQL clientを導入し、Aurora cluster endpoint、port 5432、database `northwind`、user `shibtats`で接続する。接続確認では`current_user`、`current_database()`、主要テーブルの参照、管理権限を検証する。パスワードの永続保存や`.pgpass`作成は行わない。

## セキュリティと障害時の扱い

CloudShellは既存connector security groupを共有するため、接続範囲はCOA VPC内に限定される。Auroraのpublic accessは無効のままとする。CloudShell environment作成に失敗してもAurora設定は変更しない。DB role作成に失敗した場合はトランザクションをrollbackし、パスワードを含まないエラーだけを報告する。
