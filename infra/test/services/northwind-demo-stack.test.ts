// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import {
  northwindDemoStackTesting,
  NorthwindDemoStack,
} from "../../lib/stacks/services/northwind-demo-stack";

const TEST_ENV = { account: "123456789012", region: "us-east-1" };
const TEST_CONTEXT = {
  "aws:cdk:bundling-stacks": [],
  env: "dev",
  resource_prefix: "coa",
};

type Resource = {
  DeletionPolicy?: string;
  Properties: Record<string, any>;
  UpdateReplacePolicy?: string;
};

function singleResource(template: Template, type: string): [string, Resource] {
  const resources = template.findResources(type) as Record<string, Resource>;
  expect(Object.entries(resources)).toHaveLength(1);
  return Object.entries(resources)[0];
}

function lambdaRoleId(
  template: Template,
  predicate: (resource: Resource) => boolean,
): string {
  const functions = template.findResources("AWS::Lambda::Function") as Record<
    string,
    Resource
  >;
  const [, lambda] = Object.entries(functions).find(([, resource]) =>
    predicate(resource),
  )!;
  return lambda.Properties.Role["Fn::GetAtt"][0];
}

function statementsForRole(
  template: Template,
  roleId: string,
): Array<Record<string, any>> {
  const policies = template.findResources("AWS::IAM::Policy") as Record<
    string,
    Resource
  >;
  return Object.values(policies)
    .filter((policy) =>
      policy.Properties.Roles.some(
        (role: Record<string, string>) => role.Ref === roleId,
      ),
    )
    .flatMap((policy) => policy.Properties.PolicyDocument.Statement);
}

function actions(statements: Array<Record<string, any>>): string[] {
  return statements
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    )
    .sort();
}

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
        EnableCloudwatchLogsExports: ["postgresql"],
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
      const [, resource] = singleResource(template, resourceType);
      expect(resource.DeletionPolicy).toBe("Snapshot");
      expect(resource.UpdateReplacePolicy).toBe("Snapshot");
      expect(resource.Properties.Tags).toEqual(
        expect.arrayContaining([
          { Key: "Component", Value: "northwind-demo" },
          { Key: "Environment", Value: "dev" },
          { Key: "Project", Value: "semantic-context" },
          { Key: "created_by", Value: "aurora-skill" },
          { Key: "generation_model", Value: "gpt-5" },
        ]),
      );
    }
  });

  it("uses private-with-egress subnets and connector-only PostgreSQL ingress", () => {
    const [, subnetGroup] = singleResource(template, "AWS::RDS::DBSubnetGroup");
    expect(subnetGroup.Properties.SubnetIds).toHaveLength(2);
    for (const subnetId of subnetGroup.Properties.SubnetIds) {
      expect(JSON.stringify(subnetId)).toContain("PrivateSubnet");
      expect(JSON.stringify(subnetId)).not.toContain("PublicSubnet");
    }

    const ingressRules = template.findResources(
      "AWS::EC2::SecurityGroupIngress",
    ) as Record<string, Resource>;
    expect(Object.values(ingressRules)).toHaveLength(1);
    const [ingressRule] = Object.values(ingressRules);
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

  it("generates the approved cluster secret without publishing passwords", () => {
    template.hasResourceProperties(
      "AWS::SecretsManager::Secret",
      Match.objectLike({ GenerateSecretString: Match.anyValue() }),
    );
    const outputs = template.toJSON().Outputs ?? {};
    expect(Object.keys(outputs).sort()).toEqual([
      "NorthwindClusterEndpoint",
      "NorthwindDatabaseName",
      "NorthwindPort",
      "NorthwindSecretArn",
    ]);
    expect(JSON.stringify(outputs)).not.toMatch(/password/i);
    expect(JSON.stringify(outputs)).not.toContain("{{resolve:secretsmanager");
  });

  it("runs the complete seed asset through a bounded Python custom resource", () => {
    const [seedFunctionId] = Object.entries(
      template.findResources("AWS::Lambda::Function"),
    ).find(
      ([, resource]: [string, Resource]) =>
        resource.Properties.Handler === "index.handler",
    )!;
    template.hasResourceProperties(
      "AWS::Lambda::Function",
      Match.objectLike({
        FunctionName: "coa-dev-northwind-seed",
        Handler: "index.handler",
        MemorySize: 1024,
        Runtime: "python3.12",
        Timeout: 900,
      }),
    );
    const [, seed] = singleResource(
      template,
      "AWS::CloudFormation::CustomResource",
    );
    expect(seed.Properties.ClusterArn).toBeDefined();
    expect(seed.Properties.DatabaseName).toBe("northwind");
    expect(seed.Properties.SeedHash).toMatch(/^[a-f0-9]{64}$/);
    expect(seed.Properties.SecretArn).toBeDefined();
    expect(seed.Properties.ServiceToken).not.toEqual({ Ref: seedFunctionId });
  });

  it("changes SeedHash for every seed input and handler configuration", () => {
    const baseline = {
      baseData: "base-data",
      configuration: {
        handler: "index.handler",
        memorySize: 1024,
        runtime: "python3.12",
        timeoutSeconds: 900,
      },
      generator: "generator",
      handler: "handler",
      schema: "schema",
    };
    const hash = northwindDemoStackTesting.calculateSeedHash(baseline);
    const changedInputs = [
      { ...baseline, baseData: "changed-base-data" },
      { ...baseline, generator: "changed-generator" },
      { ...baseline, handler: "changed-handler" },
      { ...baseline, schema: "changed-schema" },
      {
        ...baseline,
        configuration: {
          ...baseline.configuration,
          handler: "changed.handler",
        },
      },
      {
        ...baseline,
        configuration: { ...baseline.configuration, memorySize: 2048 },
      },
      {
        ...baseline,
        configuration: { ...baseline.configuration, runtime: "python3.13" },
      },
      {
        ...baseline,
        configuration: { ...baseline.configuration, timeoutSeconds: 901 },
      },
    ];

    for (const changedInput of changedInputs) {
      expect(
        northwindDemoStackTesting.calculateSeedHash(changedInput),
      ).not.toBe(hash);
    }
    expect(northwindDemoStackTesting.calculateSeedHash(baseline)).toBe(hash);
  });

  it("scopes seed and provider permissions to the synthesized resources", () => {
    const [clusterId] = singleResource(template, "AWS::RDS::DBCluster");
    const [, seed] = singleResource(
      template,
      "AWS::CloudFormation::CustomResource",
    );
    expect(JSON.stringify(seed.Properties.ClusterArn)).toContain(clusterId);
    const [secretAttachmentId] = singleResource(
      template,
      "AWS::SecretsManager::SecretTargetAttachment",
    );
    const [seedLogGroupId] = Object.entries(
      template.findResources("AWS::Logs::LogGroup"),
    ).find(
      ([, resource]: [string, Resource]) =>
        resource.Properties.LogGroupName ===
        "/aws/lambda/coa-dev-northwind-seed",
    )!;
    const [providerLogGroupId] = Object.entries(
      template.findResources("AWS::Logs::LogGroup"),
    ).find(
      ([, resource]: [string, Resource]) =>
        resource.Properties.LogGroupName ===
        "/aws/lambda/coa-dev-northwind-seed-provider",
    )!;
    const [seedFunctionId] = Object.entries(
      template.findResources("AWS::Lambda::Function"),
    ).find(
      ([, resource]: [string, Resource]) =>
        resource.Properties.Handler === "index.handler",
    )!;
    const seedRoleId = lambdaRoleId(
      template,
      (resource) => resource.Properties.Handler === "index.handler",
    );
    const providerRoleId = lambdaRoleId(template, (resource) =>
      resource.Properties.Description?.includes(
        "AWS CDK resource provider framework",
      ),
    );
    const seedStatements = statementsForRole(template, seedRoleId);
    const providerStatements = statementsForRole(template, providerRoleId);

    expect(actions(seedStatements)).toEqual([
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "rds-data:BatchExecuteStatement",
      "rds-data:BeginTransaction",
      "rds-data:CommitTransaction",
      "rds-data:ExecuteStatement",
      "rds-data:RollbackTransaction",
      "secretsmanager:GetSecretValue",
    ]);
    expect(actions(providerStatements)).toEqual([
      "lambda:GetFunction",
      "lambda:InvokeFunction",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]);
    expect(seedStatements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Action: [
            "rds-data:BatchExecuteStatement",
            "rds-data:BeginTransaction",
            "rds-data:CommitTransaction",
            "rds-data:ExecuteStatement",
            "rds-data:RollbackTransaction",
          ],
          Resource: seed.Properties.ClusterArn,
        }),
        expect.objectContaining({
          Action: "secretsmanager:GetSecretValue",
          Resource: { Ref: secretAttachmentId },
        }),
        expect.objectContaining({
          Action: ["logs:CreateLogStream", "logs:PutLogEvents"],
          Resource: { "Fn::GetAtt": [seedLogGroupId, "Arn"] },
        }),
      ]),
    );
    expect(providerStatements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          Action: "lambda:GetFunction",
          Resource: { "Fn::GetAtt": [seedFunctionId, "Arn"] },
        }),
        expect.objectContaining({
          Action: "lambda:InvokeFunction",
          Resource: expect.arrayContaining([
            { "Fn::GetAtt": [seedFunctionId, "Arn"] },
          ]),
        }),
        expect.objectContaining({
          Action: ["logs:CreateLogStream", "logs:PutLogEvents"],
          Resource: { "Fn::GetAtt": [providerLogGroupId, "Arn"] },
        }),
      ]),
    );

    const policyResources = [...seedStatements, ...providerStatements].flatMap(
      (statement) =>
        Array.isArray(statement.Resource)
          ? statement.Resource
          : [statement.Resource],
    );
    expect(policyResources).not.toContain("*");
    expect(actions([...seedStatements, ...providerStatements])).not.toContain(
      "logs:CreateLogGroup",
    );
  });
});
