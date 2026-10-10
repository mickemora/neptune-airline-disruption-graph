#!/usr/bin/env python3
"""Command-line client for the deployed graph API Lambda.

Examples:
  python scripts/graph.py diagnose                # can the Lambda reach Neptune? any load jobs?
  python scripts/graph.py load                    # start bulk load, then poll until done
  python scripts/graph.py query summary
  python scripts/graph.py query hub_connections --params '{"limit": 5}'
  python scripts/graph.py query aircraft_rotation --params '{"tail": "N901DM"}'
  python scripts/graph.py cypher "MATCH (f:Flight {origin:'DAL'}) RETURN count(f) AS n"
  python scripts/graph.py impact WN1234 --delay 180
  python scripts/graph.py impact WN1234 --delay 180 --json   # full raw report
  python scripts/graph.py visualize impact WN1234 --delay 180  # interactive HTML, opens browser
  python scripts/graph.py visualize network                    # route map

Uses your default AWS credentials/region (AWS_PROFILE / AWS_REGION are honoured).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

import boto3
from botocore.config import Config

import visualize

STACK = "NeptuneAirlineDisruptionStack"


def function_name() -> str:
    outputs = boto3.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "GraphApiFunctionName")


def invoke(payload: dict) -> object:
    # Wait as long as the function may run (5 min) and never retry: a retried invoke
    # would run the action again (e.g. start a second bulk load).
    lam = boto3.client("lambda", config=Config(read_timeout=330, connect_timeout=10,
                                               retries={"total_max_attempts": 1}))
    resp = lam.invoke(FunctionName=function_name(), Payload=json.dumps(payload).encode())
    body = json.loads(resp["Payload"].read())
    if "FunctionError" in resp or not body.get("ok", False):
        sys.exit(f"error: {body.get('error') or body}")
    return body["result"]


def hhmm(m: int) -> str:
    day, rem = divmod(m, 1440)
    return f"{rem // 60:02d}:{rem % 60:02d}" + (f"+{day}" if day else "")


def print_table(rows) -> None:
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        print(json.dumps(rows, indent=2))
        return
    cols = list(rows[0])
    cells = [[json.dumps(r.get(c)) if isinstance(r.get(c), (list, dict)) else str(r.get(c))
              for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))


def cmd_load(_args) -> None:
    ids = invoke({"action": "load"})
    print(f"nodes load {ids['nodesLoadId']}\nedges load {ids['edgesLoadId']}")
    for label in ("nodesLoadId", "edgesLoadId"):
        while True:
            status = invoke({"action": "load_status", "loadId": ids[label]})
            overall = status["overallStatus"]
            print(f"  {label[:5]}: {overall['status']} "
                  f"(records={overall.get('totalRecords', 0)}, "
                  f"errors={overall.get('parsingErrors', 0) + overall.get('insertErrors', 0)})")
            if overall["status"] not in ("LOAD_NOT_STARTED", "LOAD_IN_PROGRESS",
                                         "LOAD_IN_QUEUE"):
                break
            time.sleep(10)
        if overall["status"] != "LOAD_COMPLETED":
            print(json.dumps(status, indent=2))
            sys.exit(1)
    print("Load complete.")


def cmd_query(args) -> None:
    print_table(invoke({"action": "query", "name": args.name,
                        "params": json.loads(args.params)}))


def cmd_cypher(args) -> None:
    print_table(invoke({"action": "cypher", "query": args.query,
                        "params": json.loads(args.params)}))


def cmd_impact(args) -> None:
    r = invoke({"action": "impact", "flightId": args.flight_id, "delayMin": args.delay})
    if args.json:
        print(json.dumps(r, indent=2))
        return
    d, s = r["disruption"], r["summary"]
    print(f"\n{d['flightId']} {d['route']} (aircraft {d['tail']}) delayed {d['delayMin']} min\n")
    print(f"  Flights delayed            {s['flightsDelayed']}"
          f"  (aircraft ripple {s['aircraftRippleFlights']},"
          f" crew-induced {len(s['crewInducedFlights'])})")
    print(f"  Crew pairings affected     {s['crewPairingsAffected']}"
          f"  (duty-limit breaches {s['crewDutyBreaches']})")
    print(f"  Misconnected passengers    {s['misconnectedPassengers']}"
          f"  (A-List {s['misconnectedAListPassengers']})")

    print("\nDelayed flights")
    print_table(r["delayedFlights"])

    if r["crew"]:
        print("\nCrew pairings")
        print_table([{"pairing": p["pairingId"], "flights": " > ".join(
            f"{l['flightId']}{'*' if l['crewInduced'] else ''}" for l in p["legs"]),
            "dutyEnd": hhmm(p["projectedDutyEndMin"]), "limit": hhmm(p["dutyLimitMin"]),
            "breach": p["dutyBreach"]} for p in r["crew"]])
        print("  * crew-induced delay (crew arrives late on another aircraft)")

    if r["misconnects"]:
        print("\nMisconnects")
        print_table([{"inbound": m["inbound"], "at": m["connectAirport"],
                      "outbound": m["outbound"], "to": m["finalDest"], "pax": m["pax"],
                      "newArr": hhmm(m["newInboundArr"]), "dep": hhmm(m["newOutboundDep"]),
                      "slack": m["slackMin"]} for m in r["misconnects"]])

    if r["rebookingOptions"]:
        print("\nRebooking options (best 3 by arrival)")
        rows = []
        for req in r["rebookingOptions"]:
            for o in req["options"]:
                rows.append({"market": f"{req['origin']}-{req['dest']}",
                             "notBefore": hhmm(req["after"]),
                             "flights": "+".join(o["flights"]),
                             "dep": hhmm(o["dep"]), "arr": hhmm(o["arr"]),
                             "seats": o["seats"], "delayed": o["delayed"]})
        print_table(rows)


def cmd_visualize(args) -> None:
    if args.view == "network":
        airports = invoke({"action": "cypher", "query": visualize.NETWORK_AIRPORTS})
        routes = invoke({"action": "cypher", "query": visualize.NETWORK_ROUTES})
        graph = visualize.build_network_graph(airports, routes)
        default_name = "network.html"
    else:
        if not args.flight_id:
            sys.exit("usage: graph.py visualize impact FLIGHT_ID [--delay MIN]")
        _, graph = visualize.fetch_impact(invoke, args.flight_id, args.delay)
        default_name = f"impact-{args.flight_id}-{args.delay}.html"
    out = Path(args.out or default_name).resolve()
    out.write_text(visualize.render_html(graph), encoding="utf-8")
    print(f"Wrote {out}")
    if not args.no_open:
        webbrowser.open(out.as_uri())


def main() -> None:
    p = argparse.ArgumentParser(description="Airline disruption graph client")
    sub = p.add_subparsers(required=True)
    sub.add_parser("load").set_defaults(fn=cmd_load)
    sub.add_parser("diagnose").set_defaults(
        fn=lambda _a: print(json.dumps(invoke({"action": "diagnose"}), indent=2)))
    q = sub.add_parser("query")
    q.add_argument("name")
    q.add_argument("--params", default="{}")
    q.set_defaults(fn=cmd_query)
    c = sub.add_parser("cypher")
    c.add_argument("query")
    c.add_argument("--params", default="{}")
    c.set_defaults(fn=cmd_cypher)
    i = sub.add_parser("impact")
    i.add_argument("flight_id")
    i.add_argument("--delay", type=int, default=180)
    i.add_argument("--json", action="store_true")
    i.set_defaults(fn=cmd_impact)
    v = sub.add_parser("visualize", help="write an interactive HTML view and open it")
    v.add_argument("view", choices=["impact", "network"])
    v.add_argument("flight_id", nargs="?")
    v.add_argument("--delay", type=int, default=180)
    v.add_argument("--out", help="output HTML path (default: impact-<flight>-<delay>.html)")
    v.add_argument("--no-open", action="store_true", help="don't open the browser")
    v.set_defaults(fn=cmd_visualize)
    sub.add_parser("list").set_defaults(
        fn=lambda _a: print(json.dumps(invoke({"action": "list_queries"}), indent=2)))
    sub.add_parser("clear").set_defaults(
        fn=lambda _a: print(invoke({"action": "clear"})))
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
