"""What the agent's tools actually do: call the graph API Lambda, trim and label the results.

No agent-SDK imports here, so this module is unit-tested directly. Results are kept compact
(times as HH:MM, long lists capped) because every byte returned goes into the model's context.

Everything is read-only: the agent can analyse and look things up, never load or delete data.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

STACK = "NeptuneAirlineDisruptionStack"
MAX_ROWS = 15


class GraphError(RuntimeError):
    """The Lambda reported an error (e.g. unknown flight). Shown to the model as-is."""


# --------------------------------------------------------------------------- transport

@lru_cache(maxsize=1)
def _lambda_and_name():
    import boto3
    from botocore.config import Config
    # In AWS (the gateway tools Lambda) the function name is injected; locally it is looked up.
    name = os.getenv("GRAPH_API_FUNCTION")
    if not name:
        outputs = boto3.client("cloudformation").describe_stacks(
            StackName=STACK)["Stacks"][0]["Outputs"]
        name = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "GraphApiFunctionName")
    client = boto3.client("lambda", config=Config(read_timeout=330, connect_timeout=10,
                                                  retries={"total_max_attempts": 1}))
    return client, name


def invoke(payload: dict):
    """Call the graph API Lambda. Replaced by an in-memory stand-in in tests."""
    client, name = _lambda_and_name()
    resp = client.invoke(FunctionName=name, Payload=json.dumps(payload).encode())
    body = json.loads(resp["Payload"].read())
    if "FunctionError" in resp or not body.get("ok", False):
        raise GraphError(body.get("error") or str(body))
    return body["result"]


# --------------------------------------------------------------------------- helpers

def hhmm(minutes: int) -> str:
    day, rem = divmod(int(minutes), 1440)
    return f"{rem // 60:02d}:{rem % 60:02d}" + (f" (+{day} day)" if day else "")


def to_minutes(value: str) -> int:
    """'14:30' -> 870. Accepts H:MM or HH:MM."""
    h, m = value.strip().split(":")
    return int(h) * 60 + int(m)


def _cap(rows: list, limit: int = MAX_ROWS) -> dict:
    return {"rows": rows[:limit], "shown": min(len(rows), limit), "total": len(rows)}


def _code(value: str) -> str:
    return value.strip().upper()


# --------------------------------------------------------------------------- actions

def analyze_disruption(flight_id: str, delay_minutes: int) -> dict:
    r = invoke({"action": "impact", "flightId": _code(flight_id), "delayMin": int(delay_minutes)})
    s, d = r["summary"], r["disruption"]
    crew = [{
        "pairing": p["pairingId"],
        "crew": p["crew"],
        "flights": [f'{l["flightId"]} {l["origin"]}-{l["dest"]} (aircraft {l["tail"]}, '
                    f'+{l["effectiveDelay"]} min{", caused by crew" if l["crewInduced"] else ""})'
                    for l in p["legs"]],
        "projectedDutyEnd": hhmm(p["projectedDutyEndMin"]),
        "dutyLimit": hhmm(p["dutyLimitMin"]),
        "dutyBreach": p["dutyBreach"],
        "minutesOverLimit": max(0, p["projectedDutyEndMin"] - p["dutyLimitMin"]),
    } for p in r["crew"]]
    misconnects = [{
        "inbound": m["inbound"], "connectAt": m["connectAirport"], "missedFlight": m["outbound"],
        "finalDestination": m["finalDest"], "passengers": m["pax"], "aListPassengers": m["aListPax"],
        "newArrival": hhmm(m["newInboundArr"]), "outboundDeparts": hhmm(m["newOutboundDep"]),
        "slackMinutes": m["slackMin"],
    } for m in r["misconnects"]]
    rebooking = [{
        "market": f'{req["origin"]}-{req["dest"]}', "notBefore": hhmm(req["after"]),
        "options": [{"flights": "+".join(o["flights"]), "departs": hhmm(o["dep"]),
                     "arrives": hhmm(o["arr"]), "openSeats": o["seats"], "stops": o["stops"],
                     "alsoDelayed": o["delayed"]} for o in req["options"]],
    } for req in r["rebookingOptions"]]
    return {
        "disruption": d,
        "summary": s,
        "delayedFlights": r["delayedFlights"],
        "crewPairings": crew,
        "misconnects": _cap(misconnects),
        "rebooking": _cap(rebooking, 10),
        "assumptions": "min aircraft turn 35 min; passenger min connect 30 min; crew aircraft "
                       "change 30 min; duty limit 13 h from report (simplified); single pass "
                       "(crew-caused delays are not propagated further)",
    }


def get_flight(flight_id: str) -> dict:
    rows = invoke({"action": "query", "name": "flight", "params": {"flightId": _code(flight_id)}})
    if not rows:
        raise GraphError(f"flight {flight_id} not found")
    f = rows[0]["flight"]
    return {
        "flightId": f["flightId"], "route": f'{f["origin"]}-{f["dest"]}',
        "departs": f.get("schedDep"), "arrives": f.get("schedArr"),
        "aircraft": f["tail"], "legOfDay": f["legSeq"], "capacity": f["capacity"],
        "booked": f["booked"], "openSeats": f["seatsAvailable"], "crew": rows[0]["crew"],
    }


def get_aircraft_day(tail: str) -> dict:
    rows = invoke({"action": "query", "name": "aircraft_rotation",
                   "params": {"tail": _code(tail)}})
    if not rows:
        raise GraphError(f"aircraft {tail} not found")
    return {"aircraft": _code(tail), "legs": rows}


def find_flights(origin: str | None = None, dest: str | None = None,
                 departs_after: str = "00:00", departs_before: str = "23:59") -> dict:
    """Fixed query template; only the filters given are applied (no free-form queries)."""
    where = ["f.schedDepMin >= $after", "f.schedDepMin <= $before"]
    params = {"after": to_minutes(departs_after), "before": to_minutes(departs_before)}
    if origin:
        where.append("f.origin = $origin")
        params["origin"] = _code(origin)
    if dest:
        where.append("f.dest = $dest")
        params["dest"] = _code(dest)
    query = ("MATCH (f:Flight) WHERE " + " AND ".join(where) +
             " RETURN f.flightId AS flightId, f.origin AS origin, f.dest AS dest,"
             " f.schedDep AS departs, f.schedArr AS arrives, f.tail AS aircraft,"
             " f.seatsAvailable AS openSeats ORDER BY f.schedDepMin LIMIT 25")
    return _cap(invoke({"action": "cypher", "query": query, "params": params}), 25)


def find_rebooking(origin: str, dest: str, not_before: str) -> dict:
    req = [{"origin": _code(origin), "dest": _code(dest), "after": to_minutes(not_before)}]
    rows = invoke({"action": "query", "name": "rebook_direct", "params": {"requests": req}})
    rows += invoke({"action": "query", "name": "rebook_one_stop", "params": {"requests": req}})
    options = sorted(rows, key=lambda o: (o["arr"], len(o["flights"])))
    return _cap([{"flights": "+".join(o["flights"]), "departs": hhmm(o["dep"]),
                  "arrives": hhmm(o["arr"]), "openSeats": o["seats"],
                  "stops": len(o["flights"]) - 1} for o in options], 8)


def busiest_connections(hub: str = "DAL", limit: int = 10) -> dict:
    rows = invoke({"action": "query", "name": "hub_connections",
                   "params": {"hub": _code(hub), "limit": int(min(limit, 25))}})
    return {"hub": _code(hub), "connections": rows}
