// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { NorthwindDemoStack } from "../../lib/stacks/services";

const TEST_ENV = { account: "123456789012", region: "us-east-1" };
const TEST_CONTEXT = {
  "aws:cdk:bundling-stacks": [],
  env: "dev",
  resource_prefix: "coa",
};

describe("NorthwindDemoStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
    const stack = new NorthwindDemoStack(app, "TestNorthwind", {
      env: TEST_ENV,
      network,
    });
    template = Template.fromStack(stack);
  });

  it("creates the pinned private Aurora Serverless v2 cluster", () => {
    template.hasResourceProperties(
      "AWS::RDS::DBCluster",
      Match.objectLike({
        BackupRetentionPeriod: 7,
        DatabaseName: "northwind",
        DeletionProtection: true,
        EnableHttpEndpoint: true,
        EnableIAMDatabaseAuthentication: true,
        Engine: "aurora-postgresql",
        EngineVersion: "17.10",
        ServerlessV2ScalingConfiguration: {
          MaxCapacity: 2,
          MinCapacity: 0,
          SecondsUntilAutoPause: 300,
        },
        StorageEncrypted: true,
      }),
    );
    template.hasResourceProperties(
      "AWS::RDS::DBInstance",
      Match.objectLike({
        DBInstanceClass: "db.serverless",
        EnablePerformanceInsights: true,
        PubliclyAccessible: false,
      }),
    );
    expect(
      Object.keys(template.findResources("AWS::RDS::DBInstance")),
    ).toHaveLength(1);
    for (const resourceType of [
      "AWS::RDS::DBCluster",
      "AWS::RDS::DBInstance",
    ]) {
      template.hasResourceProperties(
        resourceType,
        Match.objectLike({
          Tags: Match.arrayWith([
            { Key: "created_by", Value: "aurora-skill" },
            { Key: "generation_model", Value: "gpt-5" },
          ]),
        }),
      );
    }
    template.hasResourceProperties(
      "AWS::RDS::DBCluster",
      Match.objectLike({ EnableCloudwatchLogsExports: ["postgresql"] }),
    );
    template.hasResource("AWS::RDS::DBCluster", {
      DeletionPolicy: "Snapshot",
      Properties: Match.anyValue(),
    });
  });

  it("uses a dedicated database security group with connector-only PostgreSQL ingress", () => {
    const ingressRules = template.findResources(
      "AWS::EC2::SecurityGroupIngress",
    );
    expect(Object.values(ingressRules)).toHaveLength(1);
    const [ingressRule] = Object.values(ingressRules) as Array<{
      Properties: Record<string, unknown>;
    }>;

    expect(ingressRule.Properties).toMatchObject({
      FromPort: 5432,
      IpProtocol: "tcp",
      ToPort: 5432,
    });
    expect(
      JSON.stringify(ingressRule.Properties.SourceSecurityGroupId),
    ).toContain("ConnectorSG");
    expect(JSON.stringify(ingressRule.Properties)).not.toContain("CidrIp");
  });

  it("generates credentials without publishing a password", () => {
    template.hasResourceProperties(
      "AWS::SecretsManager::Secret",
      Match.objectLike({ GenerateSecretString: Match.anyValue() }),
    );
    for (const outputName of [
      "NorthwindClusterEndpoint",
      "NorthwindDatabaseName",
      "NorthwindPort",
      "NorthwindSecretArn",
    ]) {
      template.hasOutput(outputName, Match.anyValue());
    }
    expect(Object.keys(template.toJSON().Outputs ?? []).sort()).toEqual([
      "NorthwindClusterEndpoint",
      "NorthwindDatabaseName",
      "NorthwindPort",
      "NorthwindSecretArn",
    ]);
    expect(JSON.stringify(template.toJSON().Outputs)).not.toMatch(/password/i);
  });

  it("runs the complete seed asset through a bounded Python custom resource", () => {
    template.hasResourceProperties(
      "AWS::Lambda::Function",
      Match.objectLike({
        Handler: "index.handler",
        MemorySize: 1024,
        Runtime: "python3.12",
        Timeout: 900,
      }),
    );
    template.hasResourceProperties(
      "AWS::CloudFormation::CustomResource",
      Match.objectLike({
        ClusterArn: Match.anyValue(),
        DatabaseName: "northwind",
        SeedHash: Match.stringLikeRegexp("^[a-f0-9]{64}$"),
        SecretArn: Match.anyValue(),
      }),
    );
  });

  it("scopes seed permissions to the Northwind cluster and generated secret", () => {
    const policies = template.findResources("AWS::IAM::Policy");
    const statements = Object.values(policies).flatMap((policy: any) =>
      policy.Properties.PolicyDocument.Statement.map(
        (statement: any) => statement,
      ),
    );
    const dataApiStatement = statements.find((statement: any) => {
      const actions = Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action];
      return actions.includes("rds-data:ExecuteStatement");
    });
    const secretStatement = statements.find((statement: any) => {
      const actions = Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action];
      return actions.includes("secretsmanager:GetSecretValue");
    });

    expect(dataApiStatement).toMatchObject({
      Action: [
        "rds-data:BatchExecuteStatement",
        "rds-data:BeginTransaction",
        "rds-data:CommitTransaction",
        "rds-data:ExecuteStatement",
        "rds-data:RollbackTransaction",
      ],
      Effect: "Allow",
    });
    expect(dataApiStatement.Resource).not.toEqual("*");
    expect(secretStatement).toMatchObject({
      Action: "secretsmanager:GetSecretValue",
      Effect: "Allow",
    });
    expect(secretStatement.Resource).not.toEqual("*");
  });
});
