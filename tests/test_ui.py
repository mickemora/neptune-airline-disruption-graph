"""Web UI: dashboard data from the in-memory graph, and a headless run of the Streamlit app."""

import sys
from pathlib import Path

import pytest

import handler
from agent import actions
from fake_graph import FakeGraph

UI = Path(__file__).resolve().parent.parent / "ui"
sys.path.insert(0, str(UI))
import backend  # noqa: E402


@pytest.fixture()
def fake_invoke(dataset, monkeypatch):
    _, out = dataset
    g = FakeGraph(out)
    monkeypatch.setattr(handler, "run", g.run)

    def invoke(payload):
        body = handler.handler(payload, None)
        if not body["ok"]:
            raise actions.GraphError(body["error"])
        return body["result"]

    monkeypatch.setattr(actions, "invoke", invoke)
    invoke.graph = g
    return invoke


def _busy_flight(g):
    morning = [f for f, v in g.flights.items() if v["dest"] == "DAL" and v["schedArrMin"] < 720]
    return max(g.connections(morning), key=lambda r: r["pax"])["inbound"]


def test_impact_view(fake_invoke):
    fid = _busy_flight(fake_invoke.graph)
    view = backend.impact_view(fid, 180, invoke=fake_invoke)
    assert view["title"].startswith(f"{fid} ") and "180 min" in view["title"]
    labels = [m[0] for m in view["metrics"]]
    assert labels == ["Flights delayed", "Crew duty breaches", "Passengers misconnected",
                      "Rebooking markets"]
    assert view["missed"] and view["rebooking"]
    assert sum(r["Passengers"] for r in view["missed"]) == view["metrics"][2][1]
    assert all(r["Short by (min)"] > 0 for r in view["missed"])
    breaches = [r for r in view["crew"] if r["Status"].startswith("Over limit")]
    assert len(breaches) == view["metrics"][1][1]
    assert "vis-network" in view["graph_html"] and 'class="embed"' in view["graph_html"]


def test_network_view(fake_invoke):
    net = backend.network_view(invoke=fake_invoke)
    assert dict((label, v) for v, label in net["stats"])["airports"] == 26


class FakeAgent:
    """Stands in for the hosted agent: answers and reports one simulated disruption."""
    def __init__(self, flight):
        self.flight = flight

    def ask(self, question):
        return {"result": f"Analyzed **{self.flight}**.",
                "toolsUsed": ["analyze_disruption", "find_rebooking"],
                "analyses": [{"flightId": self.flight, "delayMin": 180}]}


def test_streamlit_app_end_to_end(fake_invoke, monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")
    fid = _busy_flight(fake_invoke.graph)
    monkeypatch.setattr(backend, "make_agent", lambda mode: FakeAgent(fid))
    real_impact, real_network = backend.impact_view, backend.network_view
    monkeypatch.setattr(backend, "impact_view", lambda f, d: real_impact(f, d, invoke=fake_invoke))
    monkeypatch.setattr(backend, "network_view", lambda: real_network(invoke=fake_invoke))

    at = testing.AppTest.from_file(str(UI / "app.py"), default_timeout=60).run()
    assert not at.exception
    assert [m.label for m in at.metric][:1] == ["Airports"]          # route network first

    at.chat_input[0].set_value(f"{fid} is 3 hours late").run()
    assert not at.exception
    assert len(at.chat_message) == 2
    assert [m.label for m in at.metric] == ["Flights delayed", "Crew duty breaches",
                                            "Passengers misconnected", "Rebooking markets"]
    assert len(at.dataframe) == 3
    assert at.selectbox[0].value == {"flightId": fid, "delayMin": 180}
