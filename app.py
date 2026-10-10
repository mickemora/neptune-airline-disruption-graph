#!/usr/bin/env python3
"""CDK entry point.

Context overrides (cdk deploy -c key=value):
  neptuneEngineVersion   default 1.4.7.1 (Serverless needs 1.2.0.1 or later)
  minNcu / maxNcu        default 1 / 8   (Serverless floor is 1 NCU; it never scales to 0)
  graphExplorer          default true: adds a second stack with a Neptune workbench notebook
                         and Graph Explorer (ml.t3.medium, billed while running);
                         -c graphExplorer=false to skip
  notebookPlatform       default notebook-al2023-v1 (SageMaker platform identifier)
  agentCore              default true: adds the hosted AI agent stack (AgentCore Runtime,
                         Gateway, Memory); -c agentCore=false to skip. Synth builds the
                         agent's code package (build/agent_runtime.zip) when it's enabled.
  agentModelId           optional Bedrock model / inference profile for the hosted agent

Several stacks: deploy and destroy with --all (`cdk deploy --all`, `cdk destroy --all`).
"""

import os
from pathlib import Path

import aws_cdk as cdk

from datagen.generate import Generator
from scripts.build_agent_package import build as build_agent_package
from infra.agentcore_stack import AgentCoreStack
from infra.graph_explorer_stack import DEFAULT_PLATFORM, GraphExplorerStack
from infra.neptune_stack import NeptuneAirlineStack

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "generated"

# Generate the synthetic dataset on first synth so the S3 deployment has something to upload.
if not (DATA_DIR / "manifest.json").exists():
    Generator().run().write(DATA_DIR)

app = cdk.App()


def _enabled(key: str) -> bool:
    """Context flags default to on; -c key=false turns them off."""
    return str(app.node.try_get_context(key) or "true").lower() not in ("false", "0", "no", "off")


env = cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"),
                      region=os.getenv("CDK_DEFAULT_REGION"))
core = NeptuneAirlineStack(
    app, "NeptuneAirlineDisruptionStack",
    data_dir=DATA_DIR,
    engine_version=app.node.try_get_context("neptuneEngineVersion") or "1.4.7.1",
    min_ncu=float(app.node.try_get_context("minNcu") or 1),
    max_ncu=float(app.node.try_get_context("maxNcu") or 8),
    env=env,
    description="Airline disruption impact graph on Amazon Neptune Serverless (synthetic data)",
)
if _enabled("graphExplorer"):
    GraphExplorerStack(
        app, "NeptuneAirlineGraphExplorerStack", core=core, env=env,
        platform=app.node.try_get_context("notebookPlatform") or DEFAULT_PLATFORM,
        description="Neptune workbench notebook with Graph Explorer for the airline graph",
    )
if _enabled("agentCore"):
    AgentCoreStack(
        app, "NeptuneAirlineAgentCoreStack", core=core, env=env,
        runtime_zip=build_agent_package(),
        model_id=app.node.try_get_context("agentModelId"),
        description="Hosted AI operations agent: AgentCore Runtime, Gateway and Memory",
    )
cdk.Tags.of(app).add("project", "neptune-airline-disruption-graph")
app.synth()
