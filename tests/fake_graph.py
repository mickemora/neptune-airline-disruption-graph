"""In-memory stand-ins for the Neptune queries used by `impact`, built from the CSVs.

Lets the impact pipeline be exercised end-to-end without a cluster. Each method
returns rows in the same shape as the matching openCypher query.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import queries
import visualize


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        clean = {}
        for k, v in r.items():
            if k.startswith(":"):            # system column, e.g. :START_ID
                clean[k[1:]] = v
                continue
            name, _, typ = k.partition(":")
            if typ in ("Int", "Long"):
                v = int(v)
            elif typ == "Double":
                v = float(v)
            clean[name or k] = v
        out.append(clean)
    return out


class FakeGraph:
    def __init__(self, data_dir: Path):
        n, e = data_dir / "nodes", data_dir / "edges"
        self.flights = {f["flightId"]: f for f in _read(n / "flights.csv")}
        self.crew = {c["crewId"]: c for c in _read(n / "crew.csv")}
        self.pax = {p["passengerId"]: p for p in _read(n / "passengers.csv")}
        self.airports = {a["code"]: a for a in _read(n / "airports.csv")}
        self.routes = _read(e / "routes.csv")
        self.assign = _read(e / "assigned_to.csv")
        self.bookings = defaultdict(list)            # passengerId -> [flightId]
        for b in _read(e / "booked_on.csv"):
            self.bookings[b["START_ID"][3:]].append(b["END_ID"][3:])
        rules = n / "business_rules.csv"
        self.rules = _read(rules) if rules.exists() else []

    # FLIGHT_LEG (empty list when the flight does not exist)
    def flight_leg(self, flightId):
        f = self.flights.get(flightId)
        return [] if f is None else [{"flightId": f["flightId"], "legSeq": f["legSeq"],
                                      "origin": f["origin"], "dest": f["dest"],
                                      "dep": f["schedDepMin"], "arr": f["schedArrMin"],
                                      "tail": f["tail"]}]

    # AIRCRAFT_RIPPLE: strictly later legs (openCypher never reuses a relationship within
    # one pattern, so the disrupted flight itself is never returned)
    def ripple(self, flightId):
        f = self.flights[flightId]
        return sorted(
            ({"flightId": d["flightId"], "legSeq": d["legSeq"], "origin": d["origin"],
              "dest": d["dest"], "dep": d["schedDepMin"], "arr": d["schedArrMin"],
              "tail": d["tail"]}
             for d in self.flights.values()
             if d["tail"] == f["tail"] and d["legSeq"] > f["legSeq"]),
            key=lambda r: r["legSeq"])

    # CREW_SEQUENCES
    def crew_sequences(self, flightIds):
        ids = set(flightIds)
        crew_ids = {a["START_ID"][3:] for a in self.assign if a["END_ID"][3:] in ids}
        rows = []
        for a in self.assign:
            cid = a["START_ID"][3:]
            if cid in crew_ids:
                c, g = self.crew[cid], self.flights[a["END_ID"][3:]]
                rows.append({"crewId": cid, "name": c["name"], "role": c["role"],
                             "pairingId": c["pairingId"], "dutyLimitMin": c["dutyLimitMin"],
                             "seq": a["seq"], "flightId": g["flightId"], "origin": g["origin"],
                             "dest": g["dest"], "dep": g["schedDepMin"],
                             "arr": g["schedArrMin"], "tail": g["tail"]})
        return sorted(rows, key=lambda r: (r["crewId"], r["seq"]))

    # CONNECTIONS_FROM
    def connections(self, flightIds):
        ids = set(flightIds)
        groups = {}
        for pid, legs in self.bookings.items():
            for fid in legs:
                if fid not in ids:
                    continue
                f = self.flights[fid]
                for cid in legs:
                    c = self.flights[cid]
                    if c["origin"] == f["dest"] and c["schedDepMin"] > f["schedArrMin"]:
                        key = (fid, cid)
                        g = groups.setdefault(key, {
                            "inbound": fid, "inboundArr": f["schedArrMin"],
                            "inboundTail": f["tail"], "connectAirport": f["dest"],
                            "outbound": cid, "outboundDep": c["schedDepMin"],
                            "outboundTail": c["tail"], "finalDest": c["dest"], "pax": 0,
                            "passengerIds": [], "aListPax": 0})
                        g["pax"] += 1
                        g["passengerIds"].append(pid)
                        g["aListPax"] += self.pax[pid]["tier"].startswith("A-List")
        return list(groups.values())

    # REBOOK_DIRECT / REBOOK_ONE_STOP
    def rebook(self, requests, one_stop, minConnect=40):
        rows = []
        fl = list(self.flights.values())
        for r in requests:
            for f in fl:
                if f["origin"] != r["origin"] or f["schedDepMin"] < r["after"] \
                        or f["seatsAvailable"] <= 0:
                    continue
                if not one_stop and f["dest"] == r["dest"]:
                    rows.append({**r, "flights": [f["flightId"]], "dep": f["schedDepMin"],
                                 "arr": f["schedArrMin"], "seats": f["seatsAvailable"]})
                if one_stop and f["dest"] not in (r["dest"], r["origin"]):
                    for g in fl:
                        if g["origin"] == f["dest"] and g["dest"] == r["dest"] \
                                and g["schedDepMin"] >= f["schedArrMin"] + minConnect \
                                and g["seatsAvailable"] > 0:
                            rows.append({**r, "flights": [f["flightId"], g["flightId"]],
                                         "dep": f["schedDepMin"], "arr": g["schedArrMin"],
                                         "seats": min(f["seatsAvailable"],
                                                      g["seatsAvailable"])})
        return rows

    def _flight_rows(self, keep):
        return [{"flightId": f["flightId"], "origin": f["origin"], "dest": f["dest"],
                 "dep": f["schedDepMin"], "arr": f["schedArrMin"], "tail": f["tail"],
                 "legSeq": f["legSeq"], "seats": f["seatsAvailable"]}
                for f in self.flights.values() if keep(f)]

    def run(self, query, params=None):
        """Drop-in replacement for handler.run, dispatching on query text."""
        params = params or {}
        if query == queries.BUSINESS_RULES:
            return [{"ruleId": r["ruleId"], "value": r["value"]} for r in self.rules]
        if query == queries.FLIGHT_LEG:
            return self.flight_leg(params["flightId"])
        if query == queries.AIRCRAFT_RIPPLE:
            return self.ripple(params["flightId"])
        if query == queries.CREW_SEQUENCES:
            return self.crew_sequences(params["flightIds"])
        if query == queries.CONNECTIONS_FROM:
            return self.connections(params["flightIds"])
        if query == queries.REBOOK_DIRECT:
            return self.rebook(params["requests"], one_stop=False)
        if query == queries.REBOOK_ONE_STOP:
            return self.rebook(params["requests"], one_stop=True,
                               minConnect=params["minConnect"])
        if query == queries.FLIGHT:
            f = self.flights.get(params["flightId"])
            if f is None:
                return []
            crew = [f"{self.crew[a['START_ID'][3:]]['name']} ({self.crew[a['START_ID'][3:]]['role']})"
                    for a in self.assign if a["END_ID"][3:] == f["flightId"]]
            return [{"flight": dict(f), "crew": crew}]
        if query == queries.AIRCRAFT_ROTATION:
            legs = sorted((f for f in self.flights.values() if f["tail"] == params["tail"]),
                          key=lambda f: f["legSeq"])
            return [{"leg": f["legSeq"], "flightId": f["flightId"], "origin": f["origin"],
                     "dest": f["dest"], "dep": f["schedDep"], "arr": f["schedArr"]} for f in legs]
        if query == queries.HUB_CONNECTIONS:
            hub = params["hub"]
            ids = [fid for fid, f in self.flights.items() if f["dest"] == hub]
            rows = [{"inbound": r["inbound"], "fromAirport": self.flights[r["inbound"]]["origin"],
                     "outbound": r["outbound"], "toAirport": r["finalDest"],
                     "connectMin": r["outboundDep"] - r["inboundArr"], "pax": r["pax"]}
                    for r in self.connections(ids)]
            return sorted(rows, key=lambda r: -r["pax"])[:params["limit"]]
        if query.startswith("MATCH (f:Flight) WHERE f.schedDepMin >= $after"):
            rows = [f for f in self.flights.values()
                    if params["after"] <= f["schedDepMin"] <= params["before"]
                    and f["origin"] == params.get("origin", f["origin"])
                    and f["dest"] == params.get("dest", f["dest"])]
            rows.sort(key=lambda f: f["schedDepMin"])
            return [{"flightId": f["flightId"], "origin": f["origin"], "dest": f["dest"],
                     "departs": f["schedDep"], "arrives": f["schedArr"], "aircraft": f["tail"],
                     "openSeats": f["seatsAvailable"]} for f in rows[:25]]
        if query == visualize.FLIGHTS_BY_ID:
            ids = set(params["ids"])
            return self._flight_rows(lambda f: f["flightId"] in ids)
        if query == visualize.FLIGHTS_BY_TAIL:
            tails = set(params["tails"])
            return self._flight_rows(lambda f: f["tail"] in tails)
        if query == visualize.NETWORK_AIRPORTS:
            deps = defaultdict(int)
            for f in self.flights.values():
                deps[f["origin"]] += 1
            return [{"code": a["code"], "city": a["city"], "lat": a["lat"], "lon": a["lon"],
                     "isHub": a["isHub"] == "true", "departures": deps[a["code"]]}
                    for a in self.airports.values()]
        if query == visualize.NETWORK_ROUTES:
            return [{"a": r["START_ID"][3:], "b": r["END_ID"][3:], "distanceMi": r["distanceMi"]}
                    for r in self.routes if r["START_ID"][3:] < r["END_ID"][3:]]
        raise NotImplementedError(query)
