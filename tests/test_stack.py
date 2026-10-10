"""Synthesise the CDK stack and check the template. Skipped where aws-cdk-lib is absent."""

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

from infra.neptune_stack import NOTEBOOK_NAME, NeptuneAirlineStack  # noqa: E402


def _template(dataset, **kwargs):
    _, data_dir = dataset
    app = cdk.App()
    stack = NeptuneAirlineStack(app, "Test", data_dir=data_dir, engine_version="1.4.7.1",
                                min_ncu=1, max_ncu=8, **kwargs)
    return Template.from_stack(stack)


def test_core_resources(dataset):
    t = _template(dataset, graph_explorer=False)
    t.resource_count_is("AWS::Neptune::DBCluster", 1)
    t.has_resource_properties("AWS::Neptune::DBCluster", {
        "IamAuthEnabled": True, "StorageEncrypted": True,
        "ServerlessScalingConfiguration": {"MinCapacity": 1, "MaxCapacity": 8}})
    t.has_resource_properties("AWS::Neptune::DBInstance", {"DBInstanceClass": "db.serverless"})
    t.resource_count_is("AWS::SageMaker::NotebookInstance", 0)


def test_graph_explorer_notebook(dataset):
    t = _template(dataset, graph_explorer=True)
    t.has_resource_properties("AWS::SageMaker::NotebookInstance", {
        "NotebookInstanceName": NOTEBOOK_NAME,
        "PlatformIdentifier": "notebook-al2-v3",
        "DirectInternetAccess": "Enabled",
        "Tags": Match.array_with([Match.object_like({"Key": "aws-neptune-cluster-id"}),
                                  Match.object_like({"Key": "aws-neptune-resource-id"})]),
    })
    t.has_resource_properties("AWS::SageMaker::NotebookInstanceLifecycleConfig", {
        "NotebookInstanceLifecycleConfigName": NOTEBOOK_NAME})
    # Neptune accepts the notebook on 8182
    t.has_resource_properties("AWS::EC2::SecurityGroupIngress", {
        "FromPort": 8182, "ToPort": 8182, "Description": "Workbench notebook"})
