"""Graph side of the Databricks hand-off: governed rules in the impact analysis, loading
a staged export, and the laptop relay from the Unity Catalog volume to S3."""

import csv
import io
import shutil
import types

import pytest

import graph
import handler
from fake_graph import FakeGraph


def _graph_with_rules(dataset, tmp_path, overrides):
    _, out = dataset
    copy = tmp_path / "graph"
    shutil.copytree(out, copy)
    path = copy / "nodes" / "business_rules.csv"
    rows = list(csv.reader(path.open()))
    for r in rows[1:]:
        if r[2] in overrides:
            r[3] = overrides[r[2]]
    with path.open("w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    return FakeGraph(copy)


def _impact(monkeypatch, g, flight="WN4608", delay=120):
    monkeypatch.setattr(handler, "run", g.run)
    return handler.action_impact({"flightId": flight, "delayMin": delay})


def test_impact_applies_rules_stored_in_the_graph(dataset, tmp_path, monkeypatch):
    base = _impact(monkeypatch, FakeGraph(dataset[1]))
    assert base["rulesApplied"]["minConnectMin"] == 30
    assert base["rulesApplied"]["source"] == "BusinessRule nodes"
    stricter = _impact(monkeypatch, _graph_with_rules(dataset, tmp_path,
                                                      {"MIN_CONNECT_MIN": "45"}))
    assert stricter["rulesApplied"]["minConnectMin"] == 45
    assert (stricter["summary"]["misconnectedPassengers"]
            > base["summary"]["misconnectedPassengers"])


def test_impact_falls_back_to_defaults_without_rule_nodes(dataset, monkeypatch):
    g = FakeGraph(dataset[1])
    g.rules = []
    r = _impact(monkeypatch, g)
    assert r["rulesApplied"]["source"] == "defaults" and r["rulesApplied"]["minTurnMin"] == 35


class _Loader:
    def __init__(self):
        self.calls = []

    def start_loader_job(self, **kw):
        self.calls.append(kw)
        return {"payload": {"loadId": f"id{len(self.calls)}"}}


def test_load_accepts_a_staged_export_in_the_same_bucket(monkeypatch):
    loader = _Loader()
    monkeypatch.setattr(handler, "client", lambda: loader)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("LOADER_ROLE_ARN", "arn:aws:iam::1:role/loader")
    monkeypatch.setenv("DATA_S3_URI", "s3://data-bucket/graph")
    handler.action_load({"source": "s3://data-bucket/databricks/20261102T060000/"})
    assert [c["source"] for c in loader.calls] == [
        "s3://data-bucket/databricks/20261102T060000/nodes/",
        "s3://data-bucket/databricks/20261102T060000/edges/"]
    with pytest.raises(ValueError):
        handler.action_load({"source": "s3://someone-else/graph"})
    loader.calls.clear()
    handler.action_load({})
    assert loader.calls[0]["source"] == "s3://data-bucket/graph/nodes/"


class _Files:
    """Minimal stand-in for databricks.sdk WorkspaceClient().files."""

    def __init__(self, files):
        self.files = files

    def download(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return types.SimpleNamespace(contents=io.BytesIO(self.files[path]))

    def list_directory_contents(self, path):
        for p in sorted(self.files):
            if p.rsplit("/", 1)[0] == path:
                yield types.SimpleNamespace(path=p, name=p.rsplit("/", 1)[1], is_directory=False)


class _S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[f"{Bucket}/{Key}"] = Body


def test_stage_databricks_export_copies_the_volume_to_s3():
    base = "/Volumes/workspace/airline_gold/graph_export/current"
    ws = types.SimpleNamespace(files=_Files({
        f"{base}/manifest.json": b'{"counts": {"Flight": 234}}',
        f"{base}/nodes/flights.csv": b":ID,:LABEL\n",
        f"{base}/nodes/_notes.txt": b"ignored",
        f"{base}/edges/routes.csv": b":ID,:START_ID,:END_ID,:TYPE\n",
    }))
    s3 = _S3()
    uri, manifest = graph.stage_databricks_export("workspace", None, ws, s3, "data-bucket")
    assert manifest["counts"]["Flight"] == 234
    prefix = uri.removeprefix("s3://")
    assert sorted(s3.objects) == sorted([f"{prefix}/nodes/flights.csv",
                                         f"{prefix}/edges/routes.csv",
                                         f"{prefix}/manifest.json"])
    assert uri.startswith("s3://data-bucket/databricks/")


def test_stage_databricks_export_needs_a_complete_export():
    ws = types.SimpleNamespace(files=_Files({}))
    with pytest.raises(SystemExit, match="airline-ops-daily"):
        graph.stage_databricks_export("workspace", None, ws, _S3(), "data-bucket")
