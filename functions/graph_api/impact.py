"""Pure-Python disruption logic applied to rows returned from Neptune.

Kept free of AWS dependencies so it can be unit-tested locally. All times are
minutes after midnight (hub time), matching the generated data.
"""

from __future__ import annotations

from collections import defaultdict

MIN_TURN = 35          # aircraft minimum ground time
MIN_CONNECT = 30       # passenger minimum connect time (misconnect threshold)
CREW_MIN_CONNECT = 30  # crew changing aircraft
CREW_DEBRIEF = 15      # release time after last arrival


def propagate_aircraft_delay(legs: list[dict], initial_delay: int,
                             min_turn: int = MIN_TURN) -> dict[str, int]:
    """Push a delay down one aircraft's rotation, absorbing it with any spare turn time.

    `legs` are the disrupted flight followed by the aircraft's later legs, each with
    flightId, dep, arr and legSeq. Returns {flightId: delay_minutes} for legs with delay > 0.
    """
    legs = sorted(legs, key=lambda l: l["legSeq"])
    delays: dict[str, int] = {}
    delay = initial_delay
    prev = None
    for leg in legs:
        if prev is not None:
            ready = prev["arr"] + delay + min_turn
            delay = max(0, ready - leg["dep"])
        if delay <= 0:
            break
        delays[leg["flightId"]] = delay
        prev = leg
    return delays


def find_misconnects(rows: list[dict], delays: dict[str, int],
                     min_connect: int = MIN_CONNECT) -> list[dict]:
    """Return booked connections that no longer meet minimum connect time.

    `rows` come from the CONNECTIONS_FROM query (one row per inbound/outbound pair).
    Through passengers staying on the same aircraft are never misconnects.
    """
    out = []
    for r in rows:
        if r["inboundTail"] == r["outboundTail"]:
            continue
        new_arr = r["inboundArr"] + delays.get(r["inbound"], 0)
        new_dep = r["outboundDep"] + delays.get(r["outbound"], 0)
        slack = new_dep - new_arr
        if slack < min_connect:
            out.append({**r, "newInboundArr": new_arr, "newOutboundDep": new_dep,
                        "slackMin": slack})
    return sorted(out, key=lambda r: (-r["pax"], r["inbound"], r["outbound"]))


def crew_impacts(rows: list[dict], delays: dict[str, int],
                 min_connect: int = CREW_MIN_CONNECT,
                 debrief: int = CREW_DEBRIEF) -> list[dict]:
    """Walk each affected crew pairing and report knock-on delays and duty breaches.

    A crew that changes aircraft can delay a flight the aircraft ripple never touched
    ("crew-induced" delay). This is a single pass: crew-induced delays are reported,
    not fed back into further aircraft or crew propagation.
    """
    by_pairing: dict[str, dict] = {}
    for r in rows:
        p = by_pairing.setdefault(r["pairingId"], {
            "pairingId": r["pairingId"], "dutyLimitMin": r["dutyLimitMin"],
            "crew": {}, "legs": {}})
        p["crew"][r["crewId"]] = f'{r["name"]} ({r["role"]})'
        p["legs"][r["seq"]] = r

    results = []
    for p in by_pairing.values():
        legs = [p["legs"][k] for k in sorted(p["legs"])]
        prev, prev_delay = None, 0
        leg_report = []
        for leg in legs:
            ac_delay = delays.get(leg["flightId"], 0)
            crew_delay = 0
            if prev is not None:
                ready = prev["arr"] + prev_delay + min_connect
                crew_delay = max(0, ready - leg["dep"])
            eff = max(ac_delay, crew_delay)
            leg_report.append({
                "flightId": leg["flightId"], "origin": leg["origin"], "dest": leg["dest"],
                "tail": leg["tail"], "aircraftDelay": ac_delay, "effectiveDelay": eff,
                "crewInduced": crew_delay > ac_delay,
            })
            prev, prev_delay = leg, eff
        duty_end = legs[-1]["arr"] + prev_delay + debrief
        breach = duty_end > p["dutyLimitMin"]
        if any(l["effectiveDelay"] > 0 for l in leg_report):
            results.append({
                "pairingId": p["pairingId"],
                "crew": sorted(p["crew"].values()),
                "legs": leg_report,
                "projectedDutyEndMin": duty_end,
                "dutyLimitMin": p["dutyLimitMin"],
                "dutyBreach": breach,
                "crewInducedFlights": [l["flightId"] for l in leg_report if l["crewInduced"]],
            })
    return sorted(results, key=lambda r: (not r["dutyBreach"], r["pairingId"]))


def rebook_requests(misconnects: list[dict], min_connect: int = MIN_CONNECT) -> list[dict]:
    """One search per (connection airport, final destination, earliest departure)."""
    reqs = {(m["connectAirport"], m["finalDest"], m["newInboundArr"] + min_connect)
            for m in misconnects}
    return [{"origin": o, "dest": d, "after": a} for o, d, a in sorted(reqs)]


def best_options(rows: list[dict], delays: dict[str, int], top: int = 3) -> list[dict]:
    """Rank rebooking options per request by projected arrival, using known delays."""
    grouped = defaultdict(list)
    for r in rows:
        flights = r["flights"]
        dep = r["dep"] + delays.get(flights[0], 0)
        arr = r["arr"] + delays.get(flights[-1], 0)
        if dep < r["after"]:
            continue
        grouped[(r["origin"], r["dest"], r["after"])].append({
            "flights": flights, "dep": dep, "arr": arr, "seats": r["seats"],
            "stops": len(flights) - 1,
            "delayed": any(f in delays for f in flights),
        })
    return [{"origin": o, "dest": d, "after": a,
             "options": sorted(v, key=lambda x: (x["arr"], x["stops"]))[:top]}
            for (o, d, a), v in sorted(grouped.items())]


def hhmm(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    day, rem = divmod(minutes, 24 * 60)
    suffix = f"+{day}" if day else ""
    return f"{sign}{rem // 60:02d}:{rem % 60:02d}{suffix}"
