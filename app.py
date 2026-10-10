#!/usr/bin/env python3
"""CDK entry point.

Context overrides (cdk deploy -c key=value):
  neptuneEngineVersion   default 1.4.7.1 (Serverless needs 1.2.0.1 or later)
  minNcu / maxNcu        default 1 / 8   (Serverless floor is 1 NCU; it never scales to 0)
  graphExplorer          default true: adds a Neptune workbench notebook with Graph Explorer
                         (ml.t3.medium, billed while running); -c graphExplorer=false to skip
"""

import os
from pathlib import Path

import aws_cdk as cdk

from datagen.generate import Generator
from infra.neptune_stack import NeptuneAirlineStack

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "generated"

# Generate the synthetic dataset on first synth so the S3 deployment has something to upload.
if not (DATA_DIR / "manifest.json").exists():
    Generator().run().write(DATA_DIR)

app = cdk.App()
NeptuneAirlineStack(
    app, "NeptuneAirlineDisruptionStack",
    data_dir=DATA_DIR,
    engine_version=app.node.try_get_context("neptuneEngineVersion") or "1.4.7.1",
    min_ncu=float(app.node.try_get_context("minNcu") or 1),
    max_ncu=float(app.node.try_get_context("maxNcu") or 8),
    graph_explorer=str(app.node.try_get_context("graphExplorer") or "true").lower()
    not in ("false", "0", "no", "off"),
    env=cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"),
                        region=os.getenv("CDK_DEFAULT_REGION")),
    description="Airline disruption impact graph on Amazon Neptune Serverless (synthetic data)",
)
cdk.Tags.of(app).add("project", "neptune-airline-disruption-graph")
app.synth()
