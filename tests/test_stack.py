"""Synthesise the CDK stacks and check the templates. Skipped where aws-cdk-lib is absent."""

import json

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

from infra.graph_explorer_stack import NOTEBOOK_NAME, GraphExplorerStack  # noqa: E402
from infra.neptune_stack import NeptuneAirlineStack  # noqa: E402


def _stacks(dataset):
    _, data_dir = dataset
    app = cdk.App()
    core = NeptuneAirlineStack(app, "Core", data_dir=data_dir, engine_version="1.4.7.1",
                               min_ncu=1, max_ncu=8)
    explorer = GraphExplorerStack(app, "Explorer", core=core)
    return Template.from_stack(core), Template.from_stack(explorer)


def test_core_resources(dataset):
    t, _ = _stacks(dataset)
    t.resource_count_is("AWS::Neptune::DBCluster", 1)
    t.has_resource_properties("AWS::Neptune::DBCluster", {
        "IamAuthEnabled": True, "StorageEncrypted": True,
        "ServerlessScalingConfiguration": {"MinCapacity": 1, "MaxCapacity": 8}})
    t.has_resource_properties("AWS::Neptune::DBInstance", {"DBInstanceClass": "db.serverless"})
    t.resource_count_is("AWS::SageMaker::NotebookInstance", 0)


def test_graph_explorer_notebook(dataset):
    _, t = _stacks(dataset)
    t.has_resource_properties("AWS::SageMaker::NotebookInstance", {
        "NotebookInstanceName": NOTEBOOK_NAME,
        "PlatformIdentifier": "notebook-al2023-v1",
        "DirectInternetAccess": "Enabled",
        "Tags": Match.array_with([Match.object_like({"Key": "aws-neptune-cluster-id"}),
                                  Match.object_like({"Key": "aws-neptune-resource-id"})]),
    })
    t.has_resource_properties("AWS::SageMaker::NotebookInstanceLifecycleConfig", {
        "NotebookInstanceLifecycleConfigName": NOTEBOOK_NAME})
    # Neptune accepts the notebook on 8182
    t.has_resource_properties("AWS::EC2::SecurityGroupIngress", {
        "FromPort": 8182, "ToPort": 8182, "Description": "Workbench notebook"})
    # the start script picks the installer the package ships
    script = json.dumps(t.find_resources("AWS::SageMaker::NotebookInstanceLifecycleConfig"))
    assert "install_jl4x.sh" in script and "al2023" in script
