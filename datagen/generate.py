"""Generate a synthetic one-day airline schedule as Neptune openCypher CSV files.

Usage:
    python -m datagen.generate [--out data/generated] [--seed 42] [--aircraft 40]

Output layout (each folder is loaded as its own Neptune bulk-load job, nodes first):
    <out>/nodes/*.csv   Airport, Aircraft, Flight, Crew, Passenger, BusinessRule
    <out>/edges/*.csv   ROUTE, DEPARTS_FROM, ARRIVES_AT, OPERATES, NEXT_LEG,
                        ASSIGNED_TO, BOOKED_ON

All times are minutes after midnight in hub (Central) time on SERVICE_DATE, which
keeps connection arithmetic simple. Everything here is synthetic; only the airport
codes and coordinates are real.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import string
from dataclasses import dataclass, field
from pathlib import Path

from datagen.airports import (
    AIRPORTS, FIRST_NAMES, FLEET_MIX, FOCUS_CITIES, HUB, LAST_NAMES, POINT_TO_POINT, TIERS,
)
from datagen.business_rules import BUSINESS_RULES, int_rule

SERVICE_DATE = "2026-11-02"
DATA_FORMAT = 2   # bump when the CSV layout changes; app.py regenerates older datasets
CARRIER = "WN"

FIRST_DEPARTURE = 6 * 60          # 06:00
LAST_DEPARTURE = 21 * 60 + 30     # 21:30
MIN_CONNECT = 40                  # minimum passenger connection used when booking
MAX_CONNECT = 150                 # longest connection a booked itinerary will have
# Operating rules come from the shared business definitions (see business_rules.py).
MIN_TURN = int_rule("MIN_TURN_MIN")                  # minimum aircraft turn
CREW_MIN_CONNECT = int_rule("CREW_MIN_CONNECT_MIN")  # crew changing aircraft
CREW_REPORT = int_rule("CREW_REPORT_MIN")            # report time before first departure
CREW_DEBRIEF = int_rule("CREW_DEBRIEF_MIN")          # release time after last arrival
MAX_DUTY = int_rule("MAX_DUTY_MIN")                  # simplified flight-duty-period limit


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def great_circle_miles(a: str, b: str) -> int:
    _, _, _, lat1, lon1 = AIRPORTS[a]
    _, _, _, lat2, lon2 = AIRPORTS[b]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * 3958.8 * math.asin(math.sqrt(h)))


def block_minutes(distance_mi: int) -> int:
    """Gate-to-gate time: ~25 min taxi/climb overhead plus ~450 mph cruise, rounded to 5."""
    raw = 25 + distance_mi / 7.5
    return int(5 * round(raw / 5))


@dataclass
class Flight:
    flight_id: str
    origin: str
    dest: str
    dep: int
    arr: int
    distance: int
    tail: str
    leg_seq: int
    capacity: int
    fa_required: int
    booked: int = 0

    @property
    def seats_available(self) -> int:
        return self.capacity - self.booked


@dataclass
class Aircraft:
    tail: str
    type: str
    seats: int
    fa_required: int
    base: str
    legs: list[Flight] = field(default_factory=list)


@dataclass
class Pairing:
    pairing_id: str
    flights: list[Flight]

    @property
    def duty_start(self) -> int:
        return self.flights[0].dep - CREW_REPORT

    @property
    def duty_limit(self) -> int:
        return self.duty_start + MAX_DUTY


class Generator:
    def __init__(self, seed: int = 42, n_aircraft: int = 40):
        self.rng = random.Random(seed)
        self.n_aircraft = n_aircraft
        self.routes: dict[str, set[str]] = {code: set() for code in AIRPORTS}
        self.aircraft: list[Aircraft] = []
        self.flights: list[Flight] = []
        self.pairings: list[Pairing] = []
        self.crew: list[dict] = []
        self.assignments: list[dict] = []
        self.passengers: list[dict] = []
        self.bookings: list[dict] = []
        self._used_flight_numbers: set[int] = set()

    # ------------------------------------------------------------------ network
    def build_routes(self) -> None:
        for code in AIRPORTS:
            if code != HUB:
                self.routes[HUB].add(code)
                self.routes[code].add(HUB)
        for a, b in POINT_TO_POINT:
            self.routes[a].add(b)
            self.routes[b].add(a)

    # ------------------------------------------------------------------ fleet + schedule
    def _flight_number(self) -> str:
        while True:
            n = self.rng.randint(100, 4999)
            if n not in self._used_flight_numbers:
                self._used_flight_numbers.add(n)
                return f"{CARRIER}{n}"

    def build_fleet(self) -> None:
        types = []
        for type_name, seats, share, fas in FLEET_MIX:
            types += [(type_name, seats, fas)] * round(share * self.n_aircraft)
        while len(types) < self.n_aircraft:
            types.append(types[0])
        types = types[: self.n_aircraft]
        self.rng.shuffle(types)

        # Roughly a quarter of the fleet overnights at the hub, the rest at spokes,
        # weighted toward focus cities.
        spokes = [c for c in AIRPORTS if c != HUB]
        weights = [3 if c in FOCUS_CITIES else 1 for c in spokes]
        for i, (type_name, seats, fas) in enumerate(types):
            base = HUB if i < self.n_aircraft // 4 else self.rng.choices(spokes, weights)[0]
            self.aircraft.append(Aircraft(f"N{901 + i}DM", type_name, seats, fas, base))

    def build_schedule(self) -> None:
        spokes = [c for c in AIRPORTS if c != HUB]
        spoke_weights = {c: (3 if c in FOCUS_CITIES else 1) for c in spokes}
        for ac in self.aircraft:
            here = ac.base
            dep = FIRST_DEPARTURE + 5 * self.rng.randint(0, 18)   # 06:00-07:30
            seq = 1
            prev_dest = None
            while dep <= LAST_DEPARTURE and seq <= 7:
                if here == HUB:
                    options = [c for c in spokes if c != prev_dest] or spokes
                    dest = self.rng.choices(options, [spoke_weights[c] for c in options])[0]
                else:
                    # sorted(): set order changes between Python processes (hash randomisation),
                    # which would make the "deterministic" schedule differ per machine.
                    p2p = [c for c in sorted(self.routes[here]) if c != HUB and c != prev_dest]
                    dest = self.rng.choice(p2p) if p2p and self.rng.random() < 0.3 else HUB
                dist = great_circle_miles(here, dest)
                arr = dep + block_minutes(dist)
                f = Flight(self._flight_number(), here, dest, dep, arr, dist, ac.tail, seq,
                           ac.seats, ac.fa_required)
                ac.legs.append(f)
                self.flights.append(f)
                prev_dest, here = here, dest
                dep = arr + MIN_TURN + 5 * self.rng.randint(0, 7)  # 35-70 min turns
                seq += 1
        self.flights.sort(key=lambda f: (f.dep, f.flight_id))

    # ------------------------------------------------------------------ crew
    def build_crew(self) -> None:
        unassigned = {f.flight_id: f for f in self.flights}
        by_origin: dict[str, list[Flight]] = {}
        for f in self.flights:
            by_origin.setdefault(f.origin, []).append(f)
        next_leg = {}
        for ac in self.aircraft:
            for a, b in zip(ac.legs, ac.legs[1:]):
                next_leg[a.flight_id] = b

        for f in self.flights:  # chronological
            if f.flight_id not in unassigned:
                continue
            legs = [f]
            del unassigned[f.flight_id]
            target_legs = self.rng.randint(3, 6)
            duty_limit = f.dep - CREW_REPORT + MAX_DUTY
            while len(legs) < target_legs:
                last = legs[-1]
                same_ac = next_leg.get(last.flight_id)
                candidates = [
                    g for g in by_origin.get(last.dest, [])
                    if g.flight_id in unassigned
                    and last.arr + CREW_MIN_CONNECT <= g.dep <= last.arr + 120
                    and g.arr + CREW_DEBRIEF <= duty_limit
                ]
                if not candidates:
                    break
                if same_ac in candidates and self.rng.random() < 0.65:
                    nxt = same_ac
                else:
                    nxt = self.rng.choice(candidates)   # crew changes aircraft
                legs.append(nxt)
                del unassigned[nxt.flight_id]
            pairing = Pairing(f"PR{len(self.pairings) + 1:04d}", legs)
            self.pairings.append(pairing)

            fa_count = max(l.fa_required for l in legs)
            roles = ["Captain", "First Officer"] + ["Flight Attendant"] * fa_count
            for role in roles:
                crew_id = f"C{len(self.crew) + 1:05d}"
                self.crew.append({
                    "id": crew_id,
                    "name": f"{self.rng.choice(FIRST_NAMES)} {self.rng.choice(LAST_NAMES)}",
                    "role": role,
                    "base": f.origin if f.origin in FOCUS_CITIES + [HUB] else HUB,
                    "pairingId": pairing.pairing_id,
                    "dutyStartMin": pairing.duty_start,
                    "dutyLimitMin": pairing.duty_limit,
                })
                for i, leg in enumerate(legs, start=1):
                    self.assignments.append({"crew": crew_id, "flight": leg.flight_id,
                                             "seq": i, "role": role})

    # ------------------------------------------------------------------ passengers
    def _new_passenger(self) -> str:
        pid = f"P{len(self.passengers) + 1:06d}"
        tier = self.rng.choices([t for t, _ in TIERS], [w for _, w in TIERS])[0]
        self.passengers.append({
            "id": pid,
            "name": f"{self.rng.choice(FIRST_NAMES)} {self.rng.choice(LAST_NAMES)}",
            "tier": tier,
        })
        return pid

    def _pnr(self) -> str:
        return "".join(self.rng.choices(string.ascii_uppercase + string.digits, k=6))

    def build_passengers(self) -> None:
        targets = {f.flight_id: int(f.capacity * self.rng.uniform(0.72, 0.95))
                   for f in self.flights}
        by_origin: dict[str, list[Flight]] = {}
        for f in self.flights:
            by_origin.setdefault(f.origin, []).append(f)

        # 1) Connecting itineraries. Heaviest at the hub, lighter at focus cities.
        for f in self.flights:
            if f.dest == HUB:
                share = self.rng.uniform(0.25, 0.45)
            elif f.dest in FOCUS_CITIES:
                share = self.rng.uniform(0.05, 0.15)
            else:
                continue
            onward = [g for g in by_origin.get(f.dest, [])
                      if f.arr + MIN_CONNECT <= g.dep <= f.arr + MAX_CONNECT
                      and g.dest != f.origin and g.tail != f.tail]
            if not onward:
                continue
            want = int(targets[f.flight_id] * share)
            for _ in range(want):
                g = self.rng.choice(onward)
                if f.booked >= targets[f.flight_id] or g.booked >= targets[g.flight_id]:
                    continue
                pid, pnr = self._new_passenger(), self._pnr()
                for seg, leg in enumerate((f, g), start=1):
                    leg.booked += 1
                    self.bookings.append({"pax": pid, "flight": leg.flight_id,
                                          "pnr": pnr, "segment": seg})

        # 2) Local (nonstop) passengers fill the remaining seats up to target.
        for f in self.flights:
            while f.booked < targets[f.flight_id]:
                pid = self._new_passenger()
                f.booked += 1
                self.bookings.append({"pax": pid, "flight": f.flight_id,
                                      "pnr": self._pnr(), "segment": 1})

    # ------------------------------------------------------------------ run + write
    def run(self) -> "Generator":
        self.build_routes()
        self.build_fleet()
        self.build_schedule()
        self.build_crew()
        self.build_passengers()
        return self

    def write(self, out: Path) -> dict:
        nodes, edges = out / "nodes", out / "edges"
        for d in (nodes, edges):
            d.mkdir(parents=True, exist_ok=True)
            for old in d.glob("*.csv"):
                old.unlink()

        def write_csv(path: Path, header: list[str], rows) -> int:
            n = 0
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                for row in rows:
                    w.writerow(row)
                    n += 1
            return n

        counts = {}
        counts["Airport"] = write_csv(
            nodes / "airports.csv",
            [":ID", ":LABEL", "code:String", "name:String", "city:String", "state:String",
             "lat:Double", "lon:Double", "isHub:Bool"],
            ([f"AP-{c}", "Airport", c, n, city, st, lat, lon, str(c == HUB).lower()]
             for c, (n, city, st, lat, lon) in AIRPORTS.items()))
        counts["Aircraft"] = write_csv(
            nodes / "aircraft.csv",
            [":ID", ":LABEL", "tail:String", "type:String", "seats:Int", "base:String"],
            ([f"AC-{a.tail}", "Aircraft", a.tail, a.type, a.seats, a.base]
             for a in self.aircraft))
        counts["Flight"] = write_csv(
            nodes / "flights.csv",
            [":ID", ":LABEL", "flightId:String", "date:String", "origin:String", "dest:String",
             "schedDepMin:Int", "schedArrMin:Int", "schedDep:String", "schedArr:String",
             "distanceMi:Int", "tail:String", "legSeq:Int", "capacity:Int", "booked:Int",
             "seatsAvailable:Int", "status:String", "estDelayMin:Int"],
            ([f"FL-{f.flight_id}", "Flight", f.flight_id, SERVICE_DATE, f.origin, f.dest,
              f.dep, f.arr, hhmm(f.dep), hhmm(f.arr), f.distance, f.tail, f.leg_seq,
              f.capacity, f.booked, f.seats_available, "SCHEDULED", 0]
             for f in self.flights))
        counts["Crew"] = write_csv(
            nodes / "crew.csv",
            [":ID", ":LABEL", "crewId:String", "name:String", "role:String", "base:String",
             "pairingId:String", "dutyStartMin:Int", "dutyLimitMin:Int"],
            ([f"CR-{c['id']}", "Crew", c["id"], c["name"], c["role"], c["base"],
              c["pairingId"], c["dutyStartMin"], c["dutyLimitMin"]] for c in self.crew))
        counts["Passenger"] = write_csv(
            nodes / "passengers.csv",
            [":ID", ":LABEL", "passengerId:String", "name:String", "tier:String"],
            ([f"PX-{p['id']}", "Passenger", p["id"], p["name"], p["tier"]]
             for p in self.passengers))
        counts["BusinessRule"] = write_csv(
            nodes / "business_rules.csv",
            [":ID", ":LABEL", "ruleId:String", "value:String", "unit:String",
             "description:String"],
            ([f"BR-{rid}", "BusinessRule", rid, value, unit, desc]
             for rid, value, unit, desc, _owner in BUSINESS_RULES))

        edge_header = [":ID", ":START_ID", ":END_ID", ":TYPE"]
        route_rows = []
        for a in sorted(self.routes):
            for b in sorted(self.routes[a]):
                route_rows.append([f"RT-{a}-{b}", f"AP-{a}", f"AP-{b}", "ROUTE",
                                   great_circle_miles(a, b)])
        counts["ROUTE"] = write_csv(edges / "routes.csv", edge_header + ["distanceMi:Int"],
                                    route_rows)
        counts["DEPARTS_FROM"] = write_csv(
            edges / "departs_from.csv", edge_header,
            ([f"DF-{f.flight_id}", f"FL-{f.flight_id}", f"AP-{f.origin}", "DEPARTS_FROM"]
             for f in self.flights))
        counts["ARRIVES_AT"] = write_csv(
            edges / "arrives_at.csv", edge_header,
            ([f"AA-{f.flight_id}", f"FL-{f.flight_id}", f"AP-{f.dest}", "ARRIVES_AT"]
             for f in self.flights))
        counts["OPERATES"] = write_csv(
            edges / "operates.csv", edge_header + ["legSeq:Int"],
            ([f"OP-{f.flight_id}", f"AC-{f.tail}", f"FL-{f.flight_id}", "OPERATES", f.leg_seq]
             for f in self.flights))
        next_rows = []
        for ac in self.aircraft:
            for a, b in zip(ac.legs, ac.legs[1:]):
                next_rows.append([f"NL-{a.flight_id}", f"FL-{a.flight_id}", f"FL-{b.flight_id}",
                                  "NEXT_LEG", b.dep - a.arr])
        counts["NEXT_LEG"] = write_csv(edges / "next_leg.csv", edge_header + ["turnMin:Int"],
                                       next_rows)
        counts["ASSIGNED_TO"] = write_csv(
            edges / "assigned_to.csv", edge_header + ["seq:Int", "role:String"],
            ([f"AS-{a['crew']}-{a['seq']}", f"CR-{a['crew']}", f"FL-{a['flight']}",
              "ASSIGNED_TO", a["seq"], a["role"]] for a in self.assignments))
        counts["BOOKED_ON"] = write_csv(
            edges / "booked_on.csv", edge_header + ["pnr:String", "segment:Int"],
            # Keyed by record locator + segment, as a reservations system would key it.
            ([f"BK-{b['pnr']}-{b['segment']}", f"PX-{b['pax']}", f"FL-{b['flight']}",
              "BOOKED_ON", b["pnr"], b["segment"]] for b in self.bookings))

        manifest = {"format": DATA_FORMAT, "serviceDate": SERVICE_DATE, "hub": HUB,
                    "counts": counts,
                    "pairings": len(self.pairings)}
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="data/generated")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--aircraft", type=int, default=40)
    args = parser.parse_args()
    manifest = Generator(args.seed, args.aircraft).run().write(Path(args.out))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
