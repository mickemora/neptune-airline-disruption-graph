"""In-memory stand-ins for the Neptune queries used by `impact`, built from the CSVs.

Lets the impact pipeline be exercised end-to-end without a cluster. Each method
returns rows in the same shape as the matching openCypher query.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import queries


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
        self.assign = _read(e / "assigned_to.csv")
        self.bookings = defaultdict(list)            # passengerId -> [flightId]
        for b in _read(e / "booked_on.csv"):
            self.bookings[b["START_ID"][3:]].append(b["END_ID"][3:])

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

    def run(self, query, params=None):
        """Drop-in replacement for handler.run, dispatching on query text."""
        params = params or {}
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
        raise NotImplementedError(query)
