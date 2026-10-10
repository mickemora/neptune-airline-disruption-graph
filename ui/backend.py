"""Everything the web UI does that isn't layout: talk to the agent, fetch dashboard data.

Kept free of Streamlit so it can be unit-tested with the in-memory graph.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent import actions  # noqa: E402
from agent.actions import hhmm  # noqa: E402
from agent.turns import analyses, tools_used  # noqa: E402
import visualize  # noqa: E402

HOSTED = "Hosted on AgentCore"
LOCAL = "Local (this laptop)"


# --------------------------------------------------------------------------- agent

class HostedAgent:
    """The agent deployed on AgentCore Runtime; one instance = one conversation."""

    def __init__(self):
        from agent.chat import RemoteAgent
        self._remote = RemoteAgent()

    def ask(self, question: str) -> dict:
        return self._remote.ask(question)


class LocalAgent:
    """The same agent running in this process (Strands + Bedrock + the graph API Lambda)."""

    def __init__(self):
        from agent.chat import build_agent
        self._agent = build_agent(callback_handler=None)

    def ask(self, question: str) -> dict:
        start = len(self._agent.messages)
        result = self._agent(question)
        return {"result": str(result), "toolsUsed": tools_used(self._agent.messages, start),
                "analyses": analyses(self._agent.messages, start)}


def make_agent(mode: str):
    return HostedAgent() if mode == HOSTED else LocalAgent()


# --------------------------------------------------------------------------- dashboard

def impact_view(flight_id: str, delay_min: int, invoke=None) -> dict:
    """Numbers, drawing and tables for one simulated disruption."""
    report, graph = visualize.fetch_impact(invoke or actions.invoke, flight_id, delay_min)
    s, d = report["summary"], report["disruption"]
    crew = [{
        "Crew pairing": p["pairingId"],
        "Flights": " → ".join(l["flightId"] + (" (crew-caused)" if l["crewInduced"] else "")
                              for l in p["legs"]),
        "Projected duty end": hhmm(p["projectedDutyEndMin"]),
        "Duty limit": hhmm(p["dutyLimitMin"]),
        "Status": (f"Over limit by {p['projectedDutyEndMin'] - p['dutyLimitMin']} min"
                   if p["dutyBreach"] else "Within limit"),
    } for p in report["crew"]]
    missed = [{
        "Passengers": m["pax"], "A-List": m["aListPax"],
        "Inbound": m["inbound"], "At": m["connectAirport"], "New arrival": hhmm(m["newInboundArr"]),
        "Missed flight": m["outbound"], "Departs": hhmm(m["newOutboundDep"]),
        "To": m["finalDest"], "Short by (min)": max(0, 30 - m["slackMin"]),
    } for m in report["misconnects"]]
    rebooking = [{
        "Market": f'{req["origin"]}→{req["dest"]}', "Not before": hhmm(req["after"]),
        "Option": " + ".join(o["flights"]), "Departs": hhmm(o["dep"]), "Arrives": hhmm(o["arr"]),
        "Open seats": o["seats"], "Stops": o["stops"], "Also delayed": "Yes" if o["delayed"] else "",
    } for req in report["rebookingOptions"] for o in req["options"]]
    return {
        "title": f'{d["flightId"]} {d["route"].replace("-", "→")} delayed {d["delayMin"]} min',
        "aircraft": d["tail"],
        "metrics": [
            ("Flights delayed", s["flightsDelayed"],
             f'{len(s["crewInducedFlights"])} caused by crews changing aircraft'),
            ("Crew duty breaches", s["crewDutyBreaches"],
             f'{s["crewPairingsAffected"]} pairings affected'),
            ("Passengers misconnected", s["misconnectedPassengers"],
             f'{s["misconnectedAListPassengers"]} A-List'),
            ("Rebooking markets", len(report["rebookingOptions"]),
             f"{len(rebooking)} options found"),
        ],
        "graph_html": visualize.render_html(graph, embed=True),
        "crew": crew, "missed": missed, "rebooking": rebooking,
    }


def network_view(invoke=None) -> dict:
    invoke = invoke or actions.invoke
    graph = visualize.build_network_graph(
        invoke({"action": "cypher", "query": visualize.NETWORK_AIRPORTS}),
        invoke({"action": "cypher", "query": visualize.NETWORK_ROUTES}))
    return {"stats": graph["stats"], "graph_html": visualize.render_html(graph, embed=True)}
