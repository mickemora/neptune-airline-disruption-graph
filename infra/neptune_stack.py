"""Neptune Serverless + bulk-load pipeline + query Lambda, all in one stack."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Aws, CfnOutput, Duration, RemovalPolicy, Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_neptune as neptune,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
DATA_PREFIX = "graph"


class NeptuneAirlineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data_dir: Path,
                 engine_version: str, min_ncu: float, max_ncu: float, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------- network
        # Isolated subnets only: no NAT gateway cost. The S3 gateway endpoint is what
        # lets Neptune's bulk loader read from S3 without internet access.
        vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(
                name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)],
            gateway_endpoints={"S3": ec2.GatewayVpcEndpointOptions(
                service=ec2.GatewayVpcEndpointAwsService.S3)},
        )
        isolated = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)

        lambda_sg = ec2.SecurityGroup(self, "GraphApiSg", vpc=vpc,
                                      description="Graph API Lambda", allow_all_outbound=True)
        # Outbound must be open: the bulk loader connects from the cluster to S3 (via the
        # gateway endpoint). The subnets have no internet route, so S3 is all it can reach.
        neptune_sg = ec2.SecurityGroup(self, "NeptuneSg", vpc=vpc,
                                       description="Neptune cluster", allow_all_outbound=True)
        neptune_sg.add_ingress_rule(lambda_sg, ec2.Port.tcp(8182), "Graph API Lambda")

        # ---------------------------------------------------------------- data in S3
        bucket = s3.Bucket(
            self, "DataBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        deployment = s3deploy.BucketDeployment(
            self, "GraphData",
            sources=[s3deploy.Source.asset(str(data_dir), exclude=["manifest.json"])],
            destination_bucket=bucket,
            destination_key_prefix=DATA_PREFIX,
            memory_limit=512,
        )

        # Role the Neptune cluster assumes when the bulk loader reads from S3.
        loader_role = iam.Role(
            self, "NeptuneLoaderRole",
            assumed_by=iam.ServicePrincipal("rds.amazonaws.com"),
            description="Allows Neptune bulk loader to read graph CSVs",
        )
        bucket.grant_read(loader_role)

        # ---------------------------------------------------------------- Neptune
        subnet_group = neptune.CfnDBSubnetGroup(
            self, "SubnetGroup",
            db_subnet_group_description="Neptune airline graph",
            subnet_ids=isolated.subnet_ids,
        )
        cluster = neptune.CfnDBCluster(
            self, "Cluster",
            engine_version=engine_version,
            db_subnet_group_name=subnet_group.ref,
            vpc_security_group_ids=[neptune_sg.security_group_id],
            iam_auth_enabled=True,
            storage_encrypted=True,
            deletion_protection=False,
            serverless_scaling_configuration=neptune.CfnDBCluster.ServerlessScalingConfigurationProperty(
                min_capacity=min_ncu, max_capacity=max_ncu),
            associated_roles=[neptune.CfnDBCluster.DBClusterRoleProperty(
                role_arn=loader_role.role_arn)],
        )
        cluster.apply_removal_policy(RemovalPolicy.DESTROY)  # demo: no final snapshot

        writer = neptune.CfnDBInstance(
            self, "Writer",
            db_instance_class="db.serverless",
            db_cluster_identifier=cluster.ref,
        )
        writer.add_dependency(cluster)
        writer.apply_removal_policy(RemovalPolicy.DESTROY)

        # ---------------------------------------------------------------- graph API
        log_group = logs.LogGroup(self, "GraphApiLogs", retention=logs.RetentionDays.ONE_WEEK,
                                  removal_policy=RemovalPolicy.DESTROY)
        fn = lambda_.Function(
            self, "GraphApi",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(ROOT / "functions" / "graph_api")),
            timeout=Duration.minutes(5),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[lambda_sg],
            log_group=log_group,
            environment={
                "NEPTUNE_ENDPOINT": cluster.attr_endpoint,
                "NEPTUNE_PORT": cluster.attr_port,
                "LOADER_ROLE_ARN": loader_role.role_arn,
                "DATA_S3_URI": f"s3://{bucket.bucket_name}/{DATA_PREFIX}",
            },
        )
        fn.node.add_dependency(writer)
        fn.node.add_dependency(deployment)

        # IAM data-plane permissions are scoped to this cluster's resource ID.
        cluster_data_arn = (f"arn:{Aws.PARTITION}:neptune-db:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                            f"{cluster.attr_cluster_resource_id}/*")
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "neptune-db:ReadDataViaQuery",
                "neptune-db:WriteDataViaQuery",
                "neptune-db:DeleteDataViaQuery",
                "neptune-db:StartLoaderJob",
                "neptune-db:GetLoaderJobStatus",
                "neptune-db:ListLoaderJobs",
                "neptune-db:GetEngineStatus",
            ],
            resources=[cluster_data_arn],
        ))

        # Exposed for the optional Graph Explorer stack (infra/graph_explorer_stack.py).
        self.vpc = vpc
        self.subnet_id = isolated.subnet_ids[0]
        self.neptune_sg = neptune_sg
        self.cluster = cluster
        self.writer = writer
        self.cluster_data_arn = cluster_data_arn
        self.loader_role = loader_role
        self.graph_api_fn = fn

        # ---------------------------------------------------------------- outputs
        CfnOutput(self, "GraphApiFunctionName", value=fn.function_name)
        CfnOutput(self, "NeptuneEndpoint", value=cluster.attr_endpoint)
        CfnOutput(self, "DataBucketName", value=bucket.bucket_name)
        CfnOutput(self, "LoaderRoleArn", value=loader_role.role_arn)
