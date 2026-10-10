"""Optional stack: a Neptune workbench notebook with Graph Explorer.

Kept separate from the core stack on purpose: if the notebook fails to create, only this
stack rolls back and the Neptune cluster (slow to create) stays up.

Modelled on AWS's own workbench template (aws/graph-notebook, neptune-workbench-stack):
the "aws-neptune-" name and the two aws-neptune-* tags make the Neptune console list the
notebook and offer "Open Graph Explorer".

Networking: the notebook gets an interface in the core stack's isolated subnet to reach
Neptune, and SageMaker's own internet access (DirectInternetAccess) to download the
workbench software. The Neptune cluster itself keeps no internet path.
"""

from __future__ import annotations

from aws_cdk import Aws, CfnOutput, CfnTag, Fn, Stack, aws_ec2 as ec2, aws_iam as iam
from aws_cdk import aws_sagemaker as sagemaker
from constructs import Construct

from infra.neptune_stack import NeptuneAirlineStack

# The Neptune console only lists notebooks (and lifecycle configs) named "aws-neptune-*".
NOTEBOOK_NAME = "aws-neptune-airline-disruption"
# Amazon Linux 2023 + JupyterLab 4. AL2 notebooks (notebook-al2-v3) can no longer be
# created since July 1, 2026.
DEFAULT_PLATFORM = "notebook-al2023-v1"

# Runs on every notebook start (the 5-minute lifecycle limit applies). Writes the connection
# settings, then runs the workbench installer from Neptune's regional bucket. The package's
# installer names have changed with platform versions, so pick what the package ships:
# an AL2023-specific installer if present, else the JupyterLab 4 one from the Neptune docs,
# else the generic one. The chosen script is echoed to the CloudWatch log for debugging.
ON_START = """#!/bin/bash
sudo -u ec2-user -i <<'EOF'
echo 'export GRAPH_NOTEBOOK_AUTH_MODE=IAM' >> ~/.bashrc
echo 'export GRAPH_NOTEBOOK_SSL=True' >> ~/.bashrc
echo 'export GRAPH_NOTEBOOK_SERVICE=neptune-db' >> ~/.bashrc
echo 'export GRAPH_NOTEBOOK_HOST=__HOST__' >> ~/.bashrc
echo 'export GRAPH_NOTEBOOK_PORT=__PORT__' >> ~/.bashrc
echo 'export NEPTUNE_LOAD_FROM_S3_ROLE_ARN=__LOADER_ROLE__' >> ~/.bashrc
echo 'export AWS_REGION=__REGION__' >> ~/.bashrc
aws s3 cp s3://__BUCKET__/graph_notebook.tar.gz /tmp/graph_notebook.tar.gz
rm -rf /tmp/graph_notebook
tar -zxf /tmp/graph_notebook.tar.gz -C /tmp
cd /tmp/graph_notebook
echo "graph_notebook package contains: $(ls *.sh 2>/dev/null | tr '\\n' ' ')"
INSTALLER=$(ls install*al2023*.sh 2>/dev/null | head -n 1)
if [ -z "$INSTALLER" ] && [ -f install_jl4x.sh ]; then INSTALLER=install_jl4x.sh; fi
if [ -z "$INSTALLER" ]; then INSTALLER=install.sh; fi
echo "Running installer: $INSTALLER"
"/tmp/graph_notebook/$INSTALLER"
EOF
"""


class GraphExplorerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, core: NeptuneAirlineStack,
                 platform: str = DEFAULT_PLATFORM,
                 instance_type: str = "ml.t3.medium", **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.add_dependency(core)
        cluster = core.cluster

        notebook_sg = ec2.SecurityGroup(self, "NotebookSg", vpc=core.vpc,
                                        description="Neptune workbench notebook",
                                        allow_all_outbound=True)
        # Created here (not on the core stack's group object) so the core stack never
        # depends on this one: deleting this stack removes the rule cleanly.
        ec2.CfnSecurityGroupIngress(
            self, "NeptuneFromNotebook",
            group_id=core.neptune_sg.security_group_id,
            source_security_group_id=notebook_sg.security_group_id,
            ip_protocol="tcp", from_port=8182, to_port=8182,
            description="Workbench notebook")

        software_bucket = f"aws-neptune-notebook-{Aws.REGION}"
        role = iam.Role(self, "NotebookRole",
                        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
                        description="Neptune workbench notebook (Graph Explorer)")
        role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[f"arn:{Aws.PARTITION}:s3:::{software_bucket}",
                       f"arn:{Aws.PARTITION}:s3:::{software_bucket}/*"]))
        role.add_to_policy(iam.PolicyStatement(
            actions=["neptune-db:*"], resources=[core.cluster_data_arn]))
        role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"arn:{Aws.PARTITION}:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                       "log-group:/aws/sagemaker/*"]))

        script = (ON_START
                  .replace("__HOST__", cluster.attr_endpoint)
                  .replace("__PORT__", cluster.attr_port)
                  .replace("__LOADER_ROLE__", core.loader_role.role_arn)
                  .replace("__REGION__", Aws.REGION)
                  .replace("__BUCKET__", software_bucket))
        lifecycle = sagemaker.CfnNotebookInstanceLifecycleConfig(
            self, "NotebookLifecycle",
            notebook_instance_lifecycle_config_name=NOTEBOOK_NAME,
            on_start=[sagemaker.CfnNotebookInstanceLifecycleConfig
                      .NotebookInstanceLifecycleHookProperty(content=Fn.base64(script))],
        )

        notebook = sagemaker.CfnNotebookInstance(
            self, "Notebook",
            notebook_instance_name=NOTEBOOK_NAME,
            instance_type=instance_type,
            platform_identifier=platform,
            role_arn=role.role_arn,
            subnet_id=core.subnet_id,
            security_group_ids=[notebook_sg.security_group_id],
            direct_internet_access="Enabled",
            lifecycle_config_name=lifecycle.attr_notebook_instance_lifecycle_config_name,
            tags=[CfnTag(key="aws-neptune-cluster-id", value=cluster.ref),
                  CfnTag(key="aws-neptune-resource-id",
                         value=cluster.attr_cluster_resource_id)],
        )
        notebook.node.add_dependency(role)

        CfnOutput(self, "NotebookName", value=notebook.attr_notebook_instance_name)
        CfnOutput(self, "GraphExplorerHowTo",
                  value=f"Neptune console > Notebooks > {NOTEBOOK_NAME} > Actions > "
                        "Open Graph Explorer")
        CfnOutput(self, "NotebookStartLog",
                  value=f"CloudWatch log group /aws/sagemaker/NotebookInstances, stream "
                        f"{NOTEBOOK_NAME}/LifecycleConfigOnStart")
