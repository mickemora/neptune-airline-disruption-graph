"""Lambda entry point: the only path into the VPC-isolated Neptune cluster.

Event shapes:
  {"action": "load"}                                   start bulk load (nodes, then edges)
  {"action": "load_status", "loadId": "..."}           loader job status (+ first errors)
  {"action": "list_queries"}
  {"action": "query", "name": "hub_connections", "params": {"limit": 5}}
  {"action": "cypher", "query": "MATCH (n) RETURN count(n)", "params": {}}
  {"action": "impact", "flightId": "WN1234", "delayMin": 180}
  {"action": "clear"}                                  delete all data (batched)

Requests are signed with SigV4 by boto3's `neptunedata` client (IAM auth is on).
"""

from __future__ import annotations

import json
import os

import boto3
from botocore.config import Config

import impact
import queries

_client = None


def client():
    global _client
    if _client is None:
        endpoint = f'https://{os.environ["NEPTUNE_ENDPOINT"]}:{os.environ["NEPTUNE_PORT"]}'
        _client = boto3.client(
            "neptunedata", endpoint_url=endpoint,
            config=Config(read_timeout=120, retries={"max_attempts": 3, "mode": "standard"}))
    return _client


def run(query: str, params: dict | None = None) -> list[dict]:
    kwargs = {"openCypherQuery": query}
    if params:
        kwargs["parameters"] = json.dumps(params)
    return client().execute_open_cypher_query(**kwargs)["results"]


# --------------------------------------------------------------------------- actions

def action_load(_event):
    region = os.environ["AWS_REGION"]
    role = os.environ["LOADER_ROLE_ARN"]
    base = os.environ["DATA_S3_URI"].rstrip("/")
    common = dict(format="opencypher", s3BucketRegion=region, iamRoleArn=role,
                  failOnError=True, parallelism="MEDIUM", queueRequest=True,
                  updateSingleCardinalityProperties=True)
    nodes = client().start_loader_job(source=f"{base}/nodes/", **common)["payload"]["loadId"]
    # Edges wait for the node job, so every :START_ID/:END_ID already exists.
    edges = client().start_loader_job(source=f"{base}/edges/", dependencies=[nodes],
                                      **common)["payload"]["loadId"]
    return {"nodesLoadId": nodes, "edgesLoadId": edges,
            "next": "invoke load_status with each loadId until LOAD_COMPLETED"}


def action_load_status(event):
    resp = client().get_loader_job_status(loadId=event["loadId"], details=True,
                                          errors=True, errorsPerPage=5)
    return resp["payload"]


def action_list_queries(_event):
    return {name: {"defaults": defaults} for name, (_, defaults) in queries.NAMED.items()}


def action_query(event):
    name = event["name"]
    if name not in queries.NAMED:
        raise ValueError(f"unknown query {name!r}; try list_queries")
    text, defaults = queries.NAMED[name]
    params = {**defaults, **(event.get("params") or {})}
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise ValueError(f"query {name!r} needs params: {missing}")
    return run(text, params)


def action_cypher(event):
    return run(event["query"], event.get("params"))


def action_impact(event):
    flight_id = event["flightId"]
    delay = int(event["delayMin"])

    # 1. Aircraft rotation ripple.
    legs = run(queries.AIRCRAFT_RIPPLE, {"flightId": flight_id})
    if not legs:
        raise ValueError(f"flight {flight_id} not found")
    delays = impact.propagate_aircraft_delay(legs, delay)

    # 2. Crew pairings on any delayed flight (may delay other aircraft).
    crew_rows = run(queries.CREW_SEQUENCES, {"flightIds": list(delays)})
    crew = impact.crew_impacts(crew_rows, delays)
    all_delays = dict(delays)
    for p in crew:
        for leg in p["legs"]:
            if leg["effectiveDelay"] > all_delays.get(leg["flightId"], 0):
                all_delays[leg["flightId"]] = leg["effectiveDelay"]

    # 3. Passenger connections out of every delayed flight.
    conn_rows = run(queries.CONNECTIONS_FROM, {"flightIds": list(all_delays)})
    misconnects = impact.find_misconnects(conn_rows, all_delays)

    # 4. Rebooking options for stranded connections (direct and one-stop).
    requests = impact.rebook_requests(misconnects)
    options = []
    if requests:
        rows = run(queries.REBOOK_DIRECT, {"requests": requests})
        rows += run(queries.REBOOK_ONE_STOP,
                    {"requests": requests, "minConnect": impact.MIN_CONNECT})
        options = impact.best_options(rows, all_delays)

    origin = next(l for l in legs if l["flightId"] == flight_id)
    misconnected_pax = sum(m["pax"] for m in misconnects)
    return {
        "disruption": {"flightId": flight_id, "tail": origin["tail"],
                       "route": f'{origin["origin"]}-{origin["dest"]}', "delayMin": delay},
        "summary": {
            "flightsDelayed": len(all_delays),
            "aircraftRippleFlights": len(delays),
            "crewInducedFlights": sorted(set(all_delays) - set(delays)),
            "crewPairingsAffected": len(crew),
            "crewDutyBreaches": sum(1 for p in crew if p["dutyBreach"]),
            "misconnectedPassengers": misconnected_pax,
            "misconnectedAListPassengers": sum(m["aListPax"] for m in misconnects),
        },
        "delayedFlights": [
            {"flightId": f, "delayMin": d, "source": "aircraft" if f in delays else "crew"}
            for f, d in sorted(all_delays.items(), key=lambda kv: -kv[1])],
        "crew": crew,
        "misconnects": [{k: v for k, v in m.items() if k != "passengerIds"}
                        | {"samplePassengerIds": m["passengerIds"][:5]} for m in misconnects],
        "rebookingOptions": options,
    }


def action_clear(_event):
    deleted = 0
    while True:
        res = run("MATCH (n) WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS n")
        n = res[0]["n"] if res else 0
        deleted += n
        if n == 0:
            return {"deletedNodes": deleted}


ACTIONS = {
    "load": action_load,
    "load_status": action_load_status,
    "list_queries": action_list_queries,
    "query": action_query,
    "cypher": action_cypher,
    "impact": action_impact,
    "clear": action_clear,
}


def handler(event, _context):
    action = event.get("action")
    if action not in ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}", "actions": sorted(ACTIONS)}
    try:
        return {"ok": True, "result": ACTIONS[action](event)}
    except Exception as exc:  # surface Neptune errors to the caller
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
