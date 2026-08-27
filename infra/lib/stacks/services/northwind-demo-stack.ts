// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { createHash } from "crypto";
import { readFileSync } from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as cr from "aws-cdk-lib/custom-resources";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import { SCLStack } from "../../constructs";
import { NetworkStack } from "../foundation/network-stack";

const SEED_RUNTIME = lambda.Runtime.PYTHON_3_12;
const SEED_TIMEOUT = cdk.Duration.minutes(15);
const SEED_MEMORY_MIB = 1024;
const SEED_FUNCTION_NAME = "northwind-seed";
const SEED_PROVIDER_FUNCTION_NAME = "northwind-seed-provider";

interface SeedHashConfiguration {
  readonly handler: string;
  readonly memorySize: number;
  readonly runtime: string;
  readonly timeoutSeconds: number;
}

interface SeedHashInputs {
  readonly baseData: string | Buffer;
  readonly configuration: SeedHashConfiguration;
  readonly generator: string | Buffer;
  readonly handler: string | Buffer;
  readonly schema: string | Buffer;
}

export interface NorthwindDemoStackProps extends cdk.StackProps {
  readonly network: NetworkStack;
}

/** Creates the private Aurora PostgreSQL database used by the Northwind demo. */
export class NorthwindDemoStack extends SCLStack {
  public readonly cluster: rds.DatabaseCluster;
  public readonly secret: secretsmanager.ISecret;

  constructor(scope: Construct, id: string, props: NorthwindDemoStackProps) {
    super(scope, id, props);
    this.addComponentTag("northwind-demo");

    const databaseSecurityGroup = new ec2.SecurityGroup(
      this,
      "DatabaseSecurityGroup",
      {
        vpc: props.network.vpc,
        securityGroupName: this.prefixed("northwind-db-sg"),
        description: "Northwind Aurora PostgreSQL security group",
        allowAllOutbound: false,
      },
    );
    databaseSecurityGroup.addIngressRule(
      props.network.connectorSecurityGroup,
      ec2.Port.tcp(5432),
      "PostgreSQL from the COA database connector",
    );

    const writer = rds.ClusterInstance.serverlessV2("Writer", {
      enablePerformanceInsights: true,
      publiclyAccessible: false,
    });
    this.cluster = new rds.DatabaseCluster(this, "Cluster", {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.of("17.10", "17"),
      }),
      writer,
      readers: [],
      vpc: props.network.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [databaseSecurityGroup],
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
    this.secret = this.cluster.secret!;

    cdk.Tags.of(this.cluster).add("created_by", "aurora-skill");
    cdk.Tags.of(this.cluster).add("generation_model", "gpt-5");

    const seedFunctionName = this.prefixed(SEED_FUNCTION_NAME);
    const seedLogGroup = new logs.LogGroup(this, "SeedHandlerLogGroup", {
      logGroupName: `/aws/lambda/${seedFunctionName}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const seedRole = lambdaRole(this, "SeedHandlerRole");
    seedLogGroup.grantWrite(seedRole);
    const seedHandler = new lambda.Function(this, "SeedHandler", {
      functionName: seedFunctionName,
      runtime: SEED_RUNTIME,
      handler: "index.handler",
      code: lambda.Code.fromAsset(seedAssetPath()),
      timeout: SEED_TIMEOUT,
      memorySize: SEED_MEMORY_MIB,
      logGroup: seedLogGroup,
      role: seedRole,
    });
    seedHandler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "rds-data:BatchExecuteStatement",
          "rds-data:BeginTransaction",
          "rds-data:CommitTransaction",
          "rds-data:ExecuteStatement",
          "rds-data:RollbackTransaction",
        ],
        resources: [this.cluster.clusterArn],
      }),
    );
    seedHandler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [this.secret.secretArn],
      }),
    );

    const providerFunctionName = this.prefixed(SEED_PROVIDER_FUNCTION_NAME);
    const providerLogGroup = new logs.LogGroup(this, "SeedProviderLogGroup", {
      logGroupName: `/aws/lambda/${providerFunctionName}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const providerRole = lambdaRole(this, "SeedProviderRole");
    providerLogGroup.grantWrite(providerRole);
    const provider = new cr.Provider(this, "SeedProvider", {
      onEventHandler: providerOnEventHandler(seedHandler),
      frameworkOnEventRole: providerRole,
      logGroup: providerLogGroup,
      providerFunctionName,
    });
    const seed = new cdk.CustomResource(this, "Seed", {
      serviceToken: provider.serviceToken,
      properties: {
        ClusterArn: this.cluster.clusterArn,
        SecretArn: this.secret.secretArn,
        DatabaseName: "northwind",
        SeedHash: seedHash(),
      },
    });
    seed.node.addDependency(this.cluster);
    seed.node.addDependency(this.cluster.node.findChild("Writer"));

    new cdk.CfnOutput(this, "NorthwindClusterEndpoint", {
      value: this.cluster.clusterEndpoint.hostname,
    });
    new cdk.CfnOutput(this, "NorthwindDatabaseName", { value: "northwind" });
    new cdk.CfnOutput(this, "NorthwindPort", {
      value: this.cluster.clusterEndpoint.port.toString(),
    });
    new cdk.CfnOutput(this, "NorthwindSecretArn", {
      value: this.secret.secretArn,
    });
  }
}

function seedAssetPath(): string {
  return path.join(__dirname, "../../lambdas/northwind-seed");
}

function seedHash(): string {
  const seedDirectory = seedAssetPath();
  return calculateSeedHash({
    baseData: readFileSync(path.join(seedDirectory, "assets/base-data.sql")),
    configuration: {
      handler: "index.handler",
      memorySize: SEED_MEMORY_MIB,
      runtime: SEED_RUNTIME.name,
      timeoutSeconds: SEED_TIMEOUT.toSeconds(),
    },
    generator: readFileSync(path.join(seedDirectory, "generator.py")),
    handler: readFileSync(path.join(seedDirectory, "index.py")),
    schema: readFileSync(path.join(seedDirectory, "assets/schema.sql")),
  });
}

function lambdaRole(scope: Construct, id: string): iam.Role {
  return new iam.Role(scope, id, {
    assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
  });
}

/**
 * The provider only invokes the unqualified seed Lambda ARN. The Lambda grant
 * helper also grants qualified ARN access, which this synchronous provider does not use.
 */
function providerOnEventHandler(handler: lambda.IFunction): lambda.IFunction {
  return {
    functionArn: handler.functionArn,
    grantInvoke: (grantee: iam.IGrantable) =>
      iam.Grant.addToPrincipal({
        grantee,
        actions: ["lambda:InvokeFunction"],
        resourceArns: [handler.functionArn],
      }),
  } as unknown as lambda.IFunction;
}

function calculateSeedHash(inputs: SeedHashInputs): string {
  const hash = createHash("sha256");
  for (const [name, value] of [
    ["assets/base-data.sql", inputs.baseData],
    ["assets/schema.sql", inputs.schema],
    ["generator.py", inputs.generator],
    ["index.py", inputs.handler],
  ] as const) {
    hash.update(name);
    hash.update("\0");
    hash.update(value);
    hash.update("\0");
  }
  hash.update(
    JSON.stringify({
      handler: inputs.configuration.handler,
      memorySize: inputs.configuration.memorySize,
      runtime: inputs.configuration.runtime,
      timeoutSeconds: inputs.configuration.timeoutSeconds,
    }),
  );
  return hash.digest("hex");
}

/** @internal Test-only access to deterministic synthesis helpers. */
export const northwindDemoStackTesting = { calculateSeedHash };
