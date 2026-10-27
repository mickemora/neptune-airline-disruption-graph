"""Write the generated day as the raw files each source system would hand over.

The Neptune generator (`generate.py`) writes graph-ready CSVs directly. Real airlines
don't have that: the data sits in separate systems, each with its own format, keys and
quirks. This module writes the same day the way those systems would export it, so the
Databricks pipeline has real integration work to do:

    reference/airports.csv              reference data (IATA codes, coordinates)
    network/routes.csv                  network planning: authorized city pairs
    fleet/aircraft_master.csv           maintenance & engineering: tails, seat configs
    scheduling/schedule_<date>.csv      SSIM-style schedule: zero-padded flight numbers,
                                        HHMM times, day offsets, equipment codes
    crew/pairings_<date>.json           crew management: nested pairings, position codes
    reservations/cdc_<date>_00N.csv     reservations change data capture: inserts,
                                        updates and deletes, LAST/FIRST names
    loyalty/members_<date>.json         loyalty platform: members only, tier codes,
                                        profile history
    ops_events/events_<date>.json       ops control event stream: estimated departures

Deliberate data-quality issues (all deterministic, so tests can count them):
  - 2 corrupt schedule rows (invalid time, origin = destination)
  - 1 retired aircraft in the fleet master
  - re-delivered duplicate CDC rows, segments moved to another flight, cancelled and
    deleted bookings, and 1 booking on a flight that isn't on the schedule
  - members with an older profile record, and lower-case tier codes
  - a flight whose estimated departure is revised twice

Usage:
    python -m datagen.sources [--out data/sources] [--seed 42]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import string
from pathlib import Path

from datagen.airports import AIRPORTS
from datagen.business_rules import tier_code
from datagen.generate import CARRIER, SERVICE_DATE, Generator, great_circle_miles

EQUIPMENT_CODES = {"737-700": "73W", "737-800": "73H", "737 MAX 8": "7M8"}
POSITION_CODES = {"Captain": "CA", "First Officer": "FO", "Flight Attendant": "FA"}
FOLDERS = ["reference", "network", "fleet", "scheduling", "crew", "reservations",
           "loyalty", "ops_events"]


def hhmm_offset(minutes: int) -> tuple[str, int]:
    day, rem = divmod(minutes, 1440)
    return f"{rem // 60:02d}{rem % 60:02d}", day


def _write_csv(path: Path, header: list[str], rows: list[list]) -> int:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def _write_jsonl(path: Path, records: list[dict]) -> int:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    return len(records)


def write_sources(gen: Generator, out: Path, seed: int = 42) -> dict:
    """Write every source file under `out`; returns a manifest of files and anomalies."""
    rng = random.Random(seed + 1)
    d = SERVICE_DATE.replace("-", "")
    for folder in FOLDERS:
        (out / folder).mkdir(parents=True, exist_ok=True)
        for old in (out / folder).iterdir():
            if old.is_file():
                old.unlink()
    files, anomalies = {}, {}
    flights = {f.flight_id: f for f in gen.flights}
    aircraft = {a.tail: a for a in gen.aircraft}
    passengers = {p["id"]: p for p in gen.passengers}

    # ---------------------------------------------------------------- reference data
    files["reference/airports.csv"] = _write_csv(
        out / "reference" / "airports.csv", ["IATA", "NAME", "CITY", "STATE", "LAT", "LON"],
        [[c, n, city, st, lat, lon] for c, (n, city, st, lat, lon) in AIRPORTS.items()])
    files["network/routes.csv"] = _write_csv(
        out / "network" / "routes.csv", ["ORIG", "DEST", "DIST_MI"],
        [[a, b, great_circle_miles(a, b)]
         for a in sorted(gen.routes) for b in sorted(gen.routes[a])])

    # ---------------------------------------------------------------- fleet master
    fleet_rows = [[a.tail, a.type, f"Y{a.seats}", a.base, "ACTIVE"] for a in gen.aircraft]
    fleet_rows.append(["N999DM", "737-700", "Y143", "DAL", "RETIRED"])
    anomalies["retiredAircraft"] = 1
    files["fleet/aircraft_master.csv"] = _write_csv(
        out / "fleet" / "aircraft_master.csv",
        ["TAIL_NUMBER", "AIRCRAFT_TYPE", "SEAT_CONFIG", "HOME_BASE", "STATUS"], fleet_rows)

    # ---------------------------------------------------------------- schedule (SSIM-ish)
    sched = []
    for f in gen.flights:
        std, _ = hhmm_offset(f.dep)
        sta, offset = hhmm_offset(f.arr)
        sched.append([CARRIER, f"{int(f.flight_id[len(CARRIER):]):04d}", d, f.origin, f.dest,
                      std, sta, offset, EQUIPMENT_CODES[aircraft[f.tail].type], f.tail,
                      f.leg_seq, f.distance])
    tail = gen.aircraft[0].tail
    sched.append([CARRIER, "5001", d, "DAL", "HOU", "2575", "2650", 0, "73W", tail, 9, 239])
    sched.append([CARRIER, "5002", d, "DAL", "DAL", "1200", "1300", 0, "73W", tail, 9, 0])
    anomalies["corruptScheduleRows"] = 2
    files[f"scheduling/schedule_{d}.csv"] = _write_csv(
        out / "scheduling" / f"schedule_{d}.csv",
        ["CARRIER", "FLT_NO", "SVC_DATE", "ORIG", "DEST", "STD", "STA", "STA_DAY_OFFSET",
         "EQUIP", "TAIL", "LEG_SEQ", "DIST_MI"], sched)

    # ---------------------------------------------------------------- crew pairings
    crew_by_pairing: dict[str, list[dict]] = {}
    for c in gen.crew:
        crew_by_pairing.setdefault(c["pairingId"], []).append(c)
    pairings = [{
        "pairing_id": p.pairing_id, "service_date": SERVICE_DATE,
        "legs": [{"seq": i, "flight": f.flight_id} for i, f in enumerate(p.flights, 1)],
        "crew": [{"employee_id": c["id"], "name": c["name"],
                  "position": POSITION_CODES[c["role"]], "base": c["base"]}
                 for c in crew_by_pairing[p.pairing_id]],
    } for p in gen.pairings]
    files[f"crew/pairings_{d}.json"] = _write_jsonl(out / "crew" / f"pairings_{d}.json",
                                                    pairings)

    # ---------------------------------------------------------------- reservations CDC
    header = ["OP", "CHANGE_SEQ", "CHANGED_AT", "RECORD_LOCATOR", "SEGMENT_NO", "CUSTOMER_ID",
              "PAX_NAME", "FLIGHT", "SVC_DATE", "SEG_STATUS"]
    seq = [100000]

    def row(op, pnr, segment, pax, flight, status, day):
        seq[0] += 1
        first, last = passengers[pax]["name"].split(" ", 1)
        return [op, seq[0], f"2026-10-{day:02d}T{seq[0] % 24:02d}:{seq[0] % 60:02d}:00Z",
                pnr, segment, pax, f"{last.upper()}/{first.upper()}", flight, SERVICE_DATE,
                status]

    used = {b["pnr"] for b in gen.bookings}

    def new_pnr() -> str:
        while True:
            pnr = "".join(rng.choices(string.ascii_uppercase + string.digits, k=6))
            if pnr not in used:
                used.add(pnr)
                return pnr

    flight_ids = sorted(flights)
    moved = set(rng.sample(range(len(gen.bookings)), len(gen.bookings) // 100))
    batch1, batch2 = [], []
    for i, b in enumerate(gen.bookings):
        if i in moved:   # booked on another flight first, then moved by an agent
            wrong = rng.choice([f for f in flight_ids if f != b["flight"]])
            batch1.append(row("I", b["pnr"], b["segment"], b["pax"], wrong, "HK", 20))
            batch2.append(row("U", b["pnr"], b["segment"], b["pax"], b["flight"], "HK", 25))
        else:
            batch1.append(row("I", b["pnr"], b["segment"], b["pax"], b["flight"], "HK", 20))
    pax_ids = sorted(passengers)
    cancelled = deleted = 0
    for k in range(150):     # trips booked and then cancelled or purged
        pnr, pax = new_pnr(), rng.choice(pax_ids)
        batch1.append(row("I", pnr, 1, pax, rng.choice(flight_ids), "HK", 21))
        if k % 2:
            batch2.append(row("U", pnr, 1, pax, batch1[-1][7], "XX", 26))
            cancelled += 1
        else:
            batch2.append(row("D", pnr, 1, pax, batch1[-1][7], "HK", 26))
            deleted += 1
    orphan_pax = rng.choice(pax_ids)
    batch1.append(row("I", new_pnr(), 1, orphan_pax, "WN9999", "HK", 22))
    duplicates = rng.sample(batch1, 100)  # the extract job re-sent some rows
    batch2.extend(duplicates)
    anomalies.update(movedSegments=len(moved), cancelledBookings=cancelled,
                     deletedBookings=deleted, unscheduledFlightBookings=1,
                     duplicateCdcRows=len(duplicates))
    files[f"reservations/cdc_{d}_001.csv"] = _write_csv(
        out / "reservations" / f"cdc_{d}_001.csv", header, batch1)
    files[f"reservations/cdc_{d}_002.csv"] = _write_csv(
        out / "reservations" / f"cdc_{d}_002.csv", header, batch2)

    # ---------------------------------------------------------------- loyalty members
    members, history, lower = [], 0, 0
    for p in gen.passengers:
        if p["tier"] == "None":
            continue                     # not a member: the loyalty system has no record
        code = tier_code(p["tier"])
        if rng.random() < 0.02:          # an older profile record with a different tier
            members.append({"customer_id": p["id"], "tier_code": "RR" if code != "RR" else "AL",
                            "enrolled_on": "2019-03-01", "updated_at": "2025-06-01T00:00:00Z"})
            history += 1
        if rng.random() < 0.01:
            code, lower = code.lower(), lower + 1
        members.append({"customer_id": p["id"], "tier_code": code,
                        "enrolled_on": f"20{rng.randint(10, 25)}-{rng.randint(1, 12):02d}-01",
                        "updated_at": "2026-10-01T00:00:00Z"})
    anomalies.update(olderProfileRecords=history, lowerCaseTierCodes=lower)
    files[f"loyalty/members_{d}.json"] = _write_jsonl(out / "loyalty" / f"members_{d}.json",
                                                      members)

    # ---------------------------------------------------------------- ops events
    candidates = [f for f in gen.flights if 8 * 60 <= f.dep <= 19 * 60]
    first = flights.get("WN4608") or candidates[0]
    others = rng.sample([f for f in candidates if f.flight_id != first.flight_id], 2)
    plan = [(first, 90, "MX", "06:05"), (others[0], 25, "ATC", "06:20"),
            (first, 120, "MX", "06:40"), (others[1], 45, "WX", "07:15")]
    events = []
    for n, (f, delay, reason, at) in enumerate(plan, 1):
        etd, _ = hhmm_offset(f.dep + delay)
        events.append({"event_id": f"EV{n:05d}", "event_type": "ETD_UPDATE",
                       "flight": f.flight_id, "svc_date": SERVICE_DATE, "etd": etd,
                       "reason": reason, "event_ts": f"{SERVICE_DATE}T{at}:00-06:00"})
    expected_delays = {f.flight_id: delay for f, delay, _, _ in plan}   # last one wins
    anomalies["revisedEstimates"] = 1
    files[f"ops_events/events_{d}.json"] = _write_jsonl(out / "ops_events" / f"events_{d}.json",
                                                        events)

    manifest = {"serviceDate": SERVICE_DATE, "files": files, "anomalies": anomalies,
                "expectedDelays": expected_delays}
    (out / "sources_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="data/sources")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--aircraft", type=int, default=40)
    args = parser.parse_args()
    gen = Generator(args.seed, args.aircraft).run()
    print(json.dumps(write_sources(gen, Path(args.out), args.seed), indent=2))


if __name__ == "__main__":
    main()
