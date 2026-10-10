"""Hosted-agent pieces: gateway tool dispatch, tool schemas, runtime package, runtime app."""

import inspect
import os
import types
import zipfile

import pytest

import handler
from agent import actions, gateway_lambda
from agent.tool_schemas import TOOLS, cfn_inline_payload
from fake_graph import FakeGraph


@pytest.fixture()
def graph(dataset, monkeypatch):
    _, out = dataset
    g = FakeGraph(out)
    monkeypatch.setattr(handler, "run", g.run)

    def fake_invoke(payload):
        body = handler.handler(payload, None)
        if not body["ok"]:
            raise actions.GraphError(body["error"])
        return body["result"]

    monkeypatch.setattr(actions, "invoke", fake_invoke)
    return g


def _context(tool):
    """Shape of the Lambda context AgentCore Gateway sends."""
    return types.SimpleNamespace(client_context=types.SimpleNamespace(
        custom={"bedrockAgentCoreToolName": f"graph___{tool}"}))


# ------------------------------------------------------------------ tool schemas

def test_every_schema_matches_an_action_signature():
    for t in TOOLS:
        fn = getattr(actions, t["name"])
        params = inspect.signature(fn).parameters
        assert set(t["properties"]) == set(params), t["name"]
        no_default = {p for p, v in params.items() if v.default is inspect.Parameter.empty}
        assert set(t["required"]) == no_default, t["name"]


def test_cfn_inline_payload_shape():
    payload = cfn_inline_payload()
    assert [p["Name"] for p in payload] == [t["name"] for t in TOOLS]
    for p in payload:
        assert p["Description"] and p["InputSchema"]["Type"] == "object"
        for prop in p["InputSchema"]["Properties"].values():
            assert prop["Type"] in ("string", "integer") and prop["Description"]


def test_tool_names_match_local_agent_tools():
    tools = pytest.importorskip("agent.tools")
    assert {t.tool_name for t in tools.ALL_TOOLS} == {t["name"] for t in TOOLS}


# ------------------------------------------------------------------ gateway Lambda

def test_gateway_lambda_dispatches_like_the_local_tools(graph):
    fid = next(iter(graph.flights))
    direct = actions.get_flight(fid)
    assert gateway_lambda.handler({"flight_id": fid}, _context("get_flight")) == direct
    r = gateway_lambda.handler({"flight_id": fid, "delay_minutes": 60},
                               _context("analyze_disruption"))
    assert r["disruption"]["flightId"] == fid


def test_gateway_lambda_errors_are_data(graph):
    assert "not found" in gateway_lambda.handler({"flight_id": "WN0"},
                                                 _context("get_flight"))["error"]
    assert "unknown tool" in gateway_lambda.handler({}, _context("drop_database"))["error"]
    # unexpected arguments are ignored rather than passed through
    r = gateway_lambda.handler({"hub": "DAL", "limit": 3, "evil": "x"},
                               _context("busiest_connections"))
    assert len(r["connections"]) == 3


# ------------------------------------------------------------------ runtime package

def test_runtime_zip_layout(tmp_path, monkeypatch):
    import scripts.build_agent_package as build
    deps = tmp_path / "deps"
    (deps / "strands").mkdir(parents=True)
    (deps / "strands" / "__init__.py").write_text("x = 1\n")
    (deps / "strands" / "__pycache__").mkdir()
    (deps / "strands" / "__pycache__" / "junk.pyc").write_bytes(b"0")
    (deps / "lib").mkdir()
    (deps / "lib" / "fast.so").write_bytes(b"\x7fELF")
    monkeypatch.setattr(build, "DEPS", deps)
    monkeypatch.setattr(build, "ZIP_PATH", tmp_path / "agent_runtime.zip")
    path = build.build_zip()
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert {"main.py", "agent/__init__.py", "agent/prompt.py",
                "strands/__init__.py", "lib/fast.so"} <= names
        assert not any("__pycache__" in n for n in names)
        assert "agent/chat.py" not in names
        assert zf.read("main.py") == build.AGENT.joinpath("runtime_app.py").read_bytes()
        mode = lambda n: (zf.getinfo(n).external_attr >> 16) & 0o777
        assert mode("main.py") == 0o644 and mode("lib/fast.so") == 0o755
    first = path.read_bytes()
    assert build.build_zip().read_bytes() == first          # deterministic -> stable asset hash


# ------------------------------------------------------------------ runtime app

def test_runtime_app_imports_and_tracks_tools(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    pytest.importorskip("mcp_proxy_for_aws")
    monkeypatch.setenv("GATEWAY_URL", "https://example.invalid/mcp")
    import importlib
    runtime_app = importlib.import_module("agent.runtime_app")
    messages = [
        {"role": "user", "content": [{"text": "old question"}]},
        {"role": "assistant", "content": [{"toolUse": {"name": "graph___get_flight"}}]},
        {"role": "user", "content": [{"text": "new question"}]},
        {"role": "assistant", "content": [{"text": "x"},
                                          {"toolUse": {"name": "graph___analyze_disruption"}}]},
    ]
    assert runtime_app._tools_used(messages, 2) == ["analyze_disruption"]
    assert runtime_app.invoke({}, types.SimpleNamespace(session_id="s"))["error"]
    os.environ.pop("GATEWAY_URL", None)
