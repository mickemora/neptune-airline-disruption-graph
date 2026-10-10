"""The agent's tools, end to end through the real Lambda handler and an in-memory graph."""

import pytest

import handler
from agent import actions
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


def _busy_morning_inbound(g):
    morning = [f for f, v in g.flights.items() if v["dest"] == "DAL" and v["schedArrMin"] < 720]
    return max(g.connections(morning), key=lambda r: r["pax"])["inbound"]


def test_time_helpers():
    assert actions.to_minutes("9:05") == 545 and actions.to_minutes("14:30") == 870
    assert actions.hhmm(545) == "09:05" and actions.hhmm(1500) == "01:00 (+1 day)"


def test_analyze_disruption_is_compact_and_labelled(graph):
    fid = _busy_morning_inbound(graph)
    r = actions.analyze_disruption(fid.lower(), 180)          # case-insensitive input
    assert r["disruption"]["flightId"] == fid
    assert r["summary"]["flightsDelayed"] == len(r["delayedFlights"]) >= 1
    assert r["misconnects"]["shown"] <= actions.MAX_ROWS
    for m in r["misconnects"]["rows"]:
        assert ":" in m["newArrival"] and "passengerIds" not in m
    for p in r["crewPairings"]:
        assert p["minutesOverLimit"] >= 0 and (p["minutesOverLimit"] > 0) == p["dutyBreach"]


def test_unknown_flight_raises_graph_error(graph):
    with pytest.raises(actions.GraphError, match="not found"):
        actions.analyze_disruption("WN0", 60)
    with pytest.raises(actions.GraphError):
        actions.get_flight("WN0")


def test_lookups(graph):
    fid = _busy_morning_inbound(graph)
    f = actions.get_flight(fid)
    assert f["flightId"] == fid and f["crew"] and f["openSeats"] >= 0
    day = actions.get_aircraft_day(f["aircraft"])
    assert fid in [leg["flightId"] for leg in day["legs"]]
    found = actions.find_flights(origin="dal", departs_after="12:00", departs_before="18:00")
    assert found["rows"] and all(r["origin"] == "DAL" and "12:00" <= r["departs"] <= "18:00"
                                 for r in found["rows"])
    conns = actions.busiest_connections("DAL", 5)
    assert len(conns["connections"]) == 5
    assert conns["connections"][0]["pax"] >= conns["connections"][-1]["pax"]


def test_find_rebooking_respects_not_before(graph):
    r = actions.find_rebooking("DAL", "ATL", "12:00")
    assert r["rows"], "expected options from DAL to ATL after noon"
    assert all(o["departs"] >= "12:00" for o in r["rows"])
    arrivals = [o["arrives"] for o in r["rows"]]
    assert arrivals == sorted(arrivals)


def test_tools_wrap_actions_when_sdk_installed(graph):
    tools = pytest.importorskip("agent.tools")
    names = {t.tool_name for t in tools.ALL_TOOLS}
    assert names == {"analyze_disruption", "get_flight", "get_aircraft_day", "find_flights",
                     "find_rebooking", "busiest_connections"}
    for t in tools.ALL_TOOLS:
        spec = t.tool_spec
        assert spec["description"] and "inputSchema" in spec
    # errors come back as data the model can read, not exceptions
    assert "error" in tools.get_flight("WN0")
