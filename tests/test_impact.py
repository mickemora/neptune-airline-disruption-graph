from collections import Counter

import handler
import impact
import queries
from fake_graph import FakeGraph


def legs(*spec):
    return [{"flightId": fid, "legSeq": i, "dep": dep, "arr": arr}
            for i, (fid, dep, arr) in enumerate(spec, start=1)]


def test_delay_absorbed_by_turn_slack():
    rotation = legs(("A", 360, 450), ("B", 500, 600), ("C", 700, 800))
    # 60 min late: B ready at 450+60+35=545 -> 45 late; C ready 600+45+35=680 < 700 -> on time
    assert impact.propagate_aircraft_delay(rotation, 60) == {"A": 60, "B": 45}


def test_delay_ripples_through_whole_rotation():
    rotation = legs(("A", 360, 450), ("B", 485, 600), ("C", 635, 700))
    assert impact.propagate_aircraft_delay(rotation, 120) == {"A": 120, "B": 120, "C": 120}


def test_misconnect_rules():
    rows = [
        {"inbound": "A", "inboundArr": 600, "inboundTail": "T1", "outbound": "X",
         "outboundDep": 660, "outboundTail": "T2", "pax": 5},
        {"inbound": "A", "inboundArr": 600, "inboundTail": "T1", "outbound": "Y",
         "outboundDep": 800, "outboundTail": "T3", "pax": 3},
        {"inbound": "A", "inboundArr": 600, "inboundTail": "T1", "outbound": "Z",
         "outboundDep": 640, "outboundTail": "T1", "pax": 9},   # same aircraft: through pax
    ]
    out = impact.find_misconnects(rows, {"A": 90})
    assert [m["outbound"] for m in out] == ["X"]
    assert out[0]["slackMin"] == -30
    # if the outbound is delayed too, the connection can survive
    assert impact.find_misconnects(rows, {"A": 90, "X": 90}) == []


def test_crew_induced_delay_and_duty_breach():
    base = {"crewId": "C1", "name": "Pat Lee", "role": "Captain", "pairingId": "P1",
            "dutyLimitMin": 900}
    rows = [
        {**base, "seq": 1, "flightId": "A", "origin": "HOU", "dest": "DAL",
         "dep": 600, "arr": 660, "tail": "T1"},
        {**base, "seq": 2, "flightId": "B", "origin": "DAL", "dest": "LAS",
         "dep": 700, "arr": 860, "tail": "T2"},   # different aircraft
    ]
    [p] = impact.crew_impacts(rows, {"A": 60})
    assert p["crewInducedFlights"] == ["B"]
    assert p["legs"][1]["effectiveDelay"] == 50          # 660+60+30-700
    assert p["projectedDutyEndMin"] == 860 + 50 + 15
    assert p["dutyBreach"] is True


def test_rebook_requests_dedupe_and_options():
    mis = [{"connectAirport": "DAL", "finalDest": "LAS", "newInboundArr": 700},
           {"connectAirport": "DAL", "finalDest": "LAS", "newInboundArr": 690}]
    assert impact.rebook_requests(mis + mis[:1]) == [
        {"origin": "DAL", "dest": "LAS", "after": 720},
        {"origin": "DAL", "dest": "LAS", "after": 730}]
    rows = [
        {"origin": "DAL", "dest": "LAS", "after": 720, "flights": ["F1"], "dep": 730,
         "arr": 900, "seats": 4},
        {"origin": "DAL", "dest": "LAS", "after": 720, "flights": ["F2", "F3"], "dep": 725,
         "arr": 880, "seats": 2},
        {"origin": "DAL", "dest": "LAS", "after": 720, "flights": ["F4"], "dep": 700,
         "arr": 850, "seats": 9},
    ]
    [req] = impact.best_options(rows, {"F2": 10})
    assert (req["origin"], req["dest"], req["after"]) == ("DAL", "LAS", 720)
    assert [o["flights"] for o in req["options"]] == [["F2", "F3"], ["F1"]]
    assert req["options"][0]["delayed"] is True


def test_impact_end_to_end_on_generated_data(dataset, monkeypatch):
    _, out = dataset
    graph = FakeGraph(out)
    monkeypatch.setattr(handler, "run", graph.run)

    # Pick the morning inbound DAL flight carrying the most connecting passengers
    # (late-evening disruptions can legitimately have no same-day rebooking options).
    morning = [f for f, v in graph.flights.items()
               if v["dest"] == "DAL" and v["schedArrMin"] < 12 * 60]
    pax_by_inbound = Counter()
    for r in graph.connections(morning):
        pax_by_inbound[r["inbound"]] += r["pax"]
    busiest = pax_by_inbound.most_common(1)[0][0]

    report = handler.action_impact({"flightId": busiest, "delayMin": 180})
    s = report["summary"]
    assert report["disruption"]["flightId"] == busiest
    assert s["flightsDelayed"] >= 1
    assert s["misconnectedPassengers"] > 0
    assert report["rebookingOptions"]
    for req in report["rebookingOptions"]:
        for o in req["options"]:
            first, last = graph.flights[o["flights"][0]], graph.flights[o["flights"][-1]]
            assert first["origin"] == req["origin"] and last["dest"] == req["dest"]
            assert o["dep"] >= req["after"]
    # No misconnect should list a through-passenger connection on the same aircraft.
    assert all(m["inboundTail"] != m["outboundTail"] for m in report["misconnects"])


def test_handler_rejects_unknown_query():
    res = handler.handler({"action": "query", "name": "nope"}, None)
    assert res["ok"] is False and "unknown query" in res["error"]
    res = handler.handler({"action": "query", "name": "flight"}, None)
    assert res["ok"] is False and "flightId" in res["error"]


def test_named_queries_reference_their_params():
    for name, (text, defaults) in queries.NAMED.items():
        for param in defaults:
            assert f"${param}" in text, (name, param)
