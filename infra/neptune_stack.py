"""Neptune Serverless + bulk-load pipeline + query Lambda (+ optional Graph Explorer)."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Aws, CfnOutput, CfnTag, Duration, Fn, RemovalPolicy, Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_neptune as neptune,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_sagemaker as sagemaker,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
DATA_PREFIX = "graph"
# The Neptune console only lists notebooks whose names start with "aws-neptune-".
NOTEBOOK_NAME = "aws-neptune-airline-disruption"


class NeptuneAirlineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data_dir: Path,
                 engine_version: str, min_ncu: float, max_ncu: float,
                 graph_explorer: bool = True, **kwargs) -> None:
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

        if graph_explorer:
            self._add_graph_explorer_notebook(vpc, isolated, neptune_sg, cluster, writer,
                                              cluster_data_arn, loader_role)

        # ---------------------------------------------------------------- outputs
        CfnOutput(self, "GraphApiFunctionName", value=fn.function_name)
        CfnOutput(self, "NeptuneEndpoint", value=cluster.attr_endpoint)
        CfnOutput(self, "DataBucketName", value=bucket.bucket_name)
        CfnOutput(self, "LoaderRoleArn", value=loader_role.role_arn)

    # -------------------------------------------------------------------- Graph Explorer
    def _add_graph_explorer_notebook(self, vpc, isolated, neptune_sg, cluster, writer,
                                     cluster_data_arn, loader_role) -> None:
        """A Neptune workbench notebook: Jupyter + graph-notebook + Graph Explorer.

        Mirrors AWS's own workbench template (aws/graph-notebook, neptune-workbench-stack):
        the "aws-neptune-" name and the two aws-neptune-* tags are what make the Neptune
        console list it and offer "Open Graph Explorer".

        Networking: the notebook gets an interface in our isolated subnet to reach Neptune,
        and SageMaker's own internet access (DirectInternetAccess) to download the
        workbench software. The Neptune cluster itself stays without any internet path.
        """
        notebook_sg = ec2.SecurityGroup(self, "NotebookSg", vpc=vpc,
                                        description="Neptune workbench notebook",
                                        allow_all_outbound=True)
        neptune_sg.add_ingress_rule(notebook_sg, ec2.Port.tcp(8182), "Workbench notebook")

        software_bucket = f"aws-neptune-notebook-{Aws.REGION}"
        role = iam.Role(
            self, "NotebookRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            description="Neptune workbench notebook (Graph Explorer)",
        )
        role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[f"arn:{Aws.PARTITION}:s3:::{software_bucket}",
                       f"arn:{Aws.PARTITION}:s3:::{software_bucket}/*"]))
        role.add_to_policy(iam.PolicyStatement(
            actions=["neptune-db:*"], resources=[cluster_data_arn]))
        role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"arn:{Aws.PARTITION}:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                       "log-group:/aws/sagemaker/*"]))

        # Start script from the Neptune docs (Amazon Linux 2, JupyterLab 4). Runs on every
        # notebook start; it writes the connection settings, then installs the workbench.
        on_start = "\n".join([
            "#!/bin/bash",
            "sudo -u ec2-user -i <<'EOF'",
            "echo 'export GRAPH_NOTEBOOK_AUTH_MODE=IAM' >> ~/.bashrc",
            "echo 'export GRAPH_NOTEBOOK_SSL=True' >> ~/.bashrc",
            "echo 'export GRAPH_NOTEBOOK_SERVICE=neptune-db' >> ~/.bashrc",
            f"echo 'export GRAPH_NOTEBOOK_HOST={cluster.attr_endpoint}' >> ~/.bashrc",
            f"echo 'export GRAPH_NOTEBOOK_PORT={cluster.attr_port}' >> ~/.bashrc",
            f"echo 'export NEPTUNE_LOAD_FROM_S3_ROLE_ARN={loader_role.role_arn}' >> ~/.bashrc",
            f"echo 'export AWS_REGION={Aws.REGION}' >> ~/.bashrc",
            f"aws s3 cp s3://{software_bucket}/graph_notebook.tar.gz /tmp/graph_notebook.tar.gz",
            "rm -rf /tmp/graph_notebook",
            "tar -zxvf /tmp/graph_notebook.tar.gz -C /tmp",
            "/tmp/graph_notebook/install_jl4x.sh",
            "EOF",
            "",
        ])
        lifecycle = sagemaker.CfnNotebookInstanceLifecycleConfig(
            self, "NotebookLifecycle",
            notebook_instance_lifecycle_config_name=NOTEBOOK_NAME,
            on_start=[sagemaker.CfnNotebookInstanceLifecycleConfig
                      .NotebookInstanceLifecycleHookProperty(content=Fn.base64(on_start))],
        )

        notebook = sagemaker.CfnNotebookInstance(
            self, "Notebook",
            notebook_instance_name=NOTEBOOK_NAME,
            instance_type="ml.t3.medium",
            platform_identifier="notebook-al2-v3",
            role_arn=role.role_arn,
            subnet_id=isolated.subnet_ids[0],
            security_group_ids=[notebook_sg.security_group_id],
            direct_internet_access="Enabled",
            lifecycle_config_name=lifecycle.attr_notebook_instance_lifecycle_config_name,
            tags=[CfnTag(key="aws-neptune-cluster-id", value=cluster.ref),
                  CfnTag(key="aws-neptune-resource-id",
                         value=cluster.attr_cluster_resource_id)],
        )
        notebook.add_dependency(writer)
        notebook.node.add_dependency(role)

        CfnOutput(self, "NotebookName", value=notebook.attr_notebook_instance_name)
        CfnOutput(self, "GraphExplorerHowTo",
                  value="Neptune console > Notebooks > select " + NOTEBOOK_NAME +
                        " > Actions > Open Graph Explorer")
