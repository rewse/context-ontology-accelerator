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
type Resources = Record<string, Resource>;

function resources(template: Template, type: string): Resources {
  return template.findResources(type) as Resources;
}

function singleResource(template: Template, type: string): [string, Resource] {
  const matchingResources = resources(template, type);
  expect(Object.entries(matchingResources)).toHaveLength(1);
  return Object.entries(matchingResources)[0];
}

function matchingResource(
  matchingResources: Resources,
  predicate: (resource: Resource) => boolean,
): [string, Resource] {
  const match = Object.entries(matchingResources).find(([, resource]) =>
    predicate(resource),
  );
  expect(match).toBeDefined();
  return match!;
}

function lambdaRoleId(
  template: Template,
  predicate: (resource: Resource) => boolean,
): string {
  const [, lambda] = matchingResource(
    resources(template, "AWS::Lambda::Function"),
    predicate,
  );
  return lambda.Properties.Role["Fn::GetAtt"][0];
}

function statementsForRole(
  template: Template,
  roleId: string,
): Array<Record<string, any>> {
  const policies = resources(template, "AWS::IAM::Policy");
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

function stringsIn(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(stringsIn);
  if (value && typeof value === "object") {
    return Object.values(value).flatMap(stringsIn);
  }
  return [];
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
      Object.keys(resources(template, "AWS::RDS::DBInstance")),
    ).toHaveLength(1);

    const [, cluster] = singleResource(template, "AWS::RDS::DBCluster");
    expect(cluster.DeletionPolicy).toBe("Snapshot");
    expect(cluster.UpdateReplacePolicy).toBe("Snapshot");
    const [, instance] = singleResource(template, "AWS::RDS::DBInstance");
    expect(instance.DeletionPolicy).toBe("Delete");
    expect(instance.UpdateReplacePolicy).toBe("Delete");
    for (const resource of [cluster, instance]) {
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

    const ingressRules = resources(template, "AWS::EC2::SecurityGroupIngress");
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
    const [seedFunctionId] = matchingResource(
      resources(template, "AWS::Lambda::Function"),
      (resource) => resource.Properties.Handler === "index.handler",
    );
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
    const [seedLogGroupId] = matchingResource(
      resources(template, "AWS::Logs::LogGroup"),
      (resource) =>
        resource.Properties.LogGroupName ===
        "/aws/lambda/coa-dev-northwind-seed",
    );
    const [providerLogGroupId] = matchingResource(
      resources(template, "AWS::Logs::LogGroup"),
      (resource) =>
        resource.Properties.LogGroupName ===
        "/aws/lambda/coa-dev-northwind-seed-provider",
    );
    for (const logGroupId of [seedLogGroupId, providerLogGroupId]) {
      expect(template.toJSON().Resources[logGroupId].DeletionPolicy).toBe(
        "Delete",
      );
      expect(
        template.toJSON().Resources[logGroupId].UpdateReplacePolicy,
      ).toBe("Delete");
    }
    const [seedFunctionId] = matchingResource(
      resources(template, "AWS::Lambda::Function"),
      (resource) => resource.Properties.Handler === "index.handler",
    );
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
          Resource: { "Fn::GetAtt": [seedFunctionId, "Arn"] },
        }),
        expect.objectContaining({
          Action: ["logs:CreateLogStream", "logs:PutLogEvents"],
          Resource: { "Fn::GetAtt": [providerLogGroupId, "Arn"] },
        }),
      ]),
    );

    const policyResources = Object.values(
      resources(template, "AWS::IAM::Policy"),
    ).flatMap((policy) =>
      policy.Properties.PolicyDocument.Statement.map(
        (statement: Record<string, unknown>) => statement.Resource,
      ),
    );
    expect(
      stringsIn(policyResources).filter((value) => value.includes("*")),
    ).toEqual([]);
    expect(actions([...seedStatements, ...providerStatements])).not.toContain(
      "logs:CreateLogGroup",
    );
  });
});
