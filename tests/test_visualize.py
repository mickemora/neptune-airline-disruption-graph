import json
import re

import handler
import visualize
from fake_graph import FakeGraph


def _graph_via_cli_path(graph, flight_id, delay):
    """Same steps as `graph.py visualize impact`, with the fake graph behind the Lambda."""
    report = handler.action_impact({"flightId": flight_id, "delayMin": delay})
    ids = visualize.impact_flight_ids(report)
    flights = graph.run(visualize.FLIGHTS_BY_ID, {"ids": ids})
    delayed = {d["flightId"] for d in report["delayedFlights"]}
    tails = sorted({f["tail"] for f in flights if f["flightId"] in delayed})
    flights += graph.run(visualize.FLIGHTS_BY_TAIL, {"tails": tails})
    return report, visualize.build_impact_graph(report, flights)


def _busiest_morning_inbound(graph):
    morning = [f for f, v in graph.flights.items()
               if v["dest"] == "DAL" and v["schedArrMin"] < 12 * 60]
    rows = graph.connections(morning)
    return max(rows, key=lambda r: r["pax"])["inbound"]


def test_impact_view_is_consistent(dataset, monkeypatch):
    _, out = dataset
    graph = FakeGraph(out)
    monkeypatch.setattr(handler, "run", graph.run)
    report, g = _graph_via_cli_path(graph, _busiest_morning_inbound(graph), 180)

    ids = [n["id"] for n in g["nodes"]]
    assert len(ids) == len(set(ids)), "duplicate node ids"
    for e in g["edges"]:
        assert e["from"] in ids and e["to"] in ids, e
    groups = {n["id"]: n["group"] for n in g["nodes"]}
    assert groups[f"fl:{report['disruption']['flightId']}"] == "disrupted"
    for d in report["delayedFlights"][1:]:
        assert groups[f"fl:{d['flightId']}"] == d["source"]
    assert sum(1 for gname in groups.values() if gname == "pax") == len(report["misconnects"])
    assert sum(1 for gname in groups.values() if gname == "pairing") == len(report["crew"])
    # the disrupted aircraft is the top lane
    top = min((n for n in g["nodes"] if n["group"] == "aircraft"), key=lambda n: n["y"])
    assert top["id"] == f"ac:{report['disruption']['tail']}"


def test_network_view(dataset):
    _, out = dataset
    graph = FakeGraph(out)
    g = visualize.build_network_graph(graph.run(visualize.NETWORK_AIRPORTS),
                                      graph.run(visualize.NETWORK_ROUTES))
    assert len(g["nodes"]) == 26 and len(g["edges"]) == 66      # 132 directed routes / 2
    dal = next(n for n in g["nodes"] if n["id"] == "DAL")
    assert dal["group"] == "hub"
    lax = next(n for n in g["nodes"] if n["id"] == "LAX")
    bwi = next(n for n in g["nodes"] if n["id"] == "BWI")
    assert lax["x"] < dal["x"] < bwi["x"]                       # west to east


def test_render_html_embeds_valid_json(dataset, monkeypatch):
    _, out = dataset
    graph = FakeGraph(out)
    monkeypatch.setattr(handler, "run", graph.run)
    _, g = _graph_via_cli_path(graph, _busiest_morning_inbound(graph), 120)
    page = visualize.render_html(g)
    data = re.search(r"const DATA = (\{.*?\});\n", page, re.S).group(1)
    parsed = json.loads(data.replace("<\\/", "</"))
    assert len(parsed["nodes"]) == len(g["nodes"])
    assert "vis-network@9.1.9" in page and "</script>" not in data
