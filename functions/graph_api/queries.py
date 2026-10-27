"""Named openCypher queries. Parameters are passed as $name and bound by Neptune."""

# --- Exploration ------------------------------------------------------------

SUMMARY = """
MATCH (n)
RETURN labels(n)[0] AS label, count(n) AS count
ORDER BY label
"""

# Governed operating rules (BusinessRule nodes, published from the lakehouse).
BUSINESS_RULES = """
MATCH (r:BusinessRule)
RETURN r.ruleId AS ruleId, r.value AS value
"""

ROUTES_FROM = """
MATCH (a:Airport {code: $airport})-[r:ROUTE]->(b:Airport)
RETURN b.code AS dest, b.city AS city, r.distanceMi AS distanceMi
ORDER BY distanceMi
"""

FLIGHT = """
MATCH (f:Flight {flightId: $flightId})
OPTIONAL MATCH (c:Crew)-[:ASSIGNED_TO]->(f)
RETURN properties(f) AS flight, collect(c.name + ' (' + c.role + ')') AS crew
"""

AIRCRAFT_ROTATION = """
MATCH (:Aircraft {tail: $tail})-[:OPERATES]->(f:Flight)
RETURN f.legSeq AS leg, f.flightId AS flightId, f.origin AS origin, f.dest AS dest,
       f.schedDep AS dep, f.schedArr AS arr
ORDER BY leg
"""

HUB_CONNECTIONS = """
MATCH (p:Passenger)-[:BOOKED_ON]->(f1:Flight)-[:ARRIVES_AT]->(:Airport {code: $hub}),
      (p)-[:BOOKED_ON]->(f2:Flight)-[:DEPARTS_FROM]->(:Airport {code: $hub})
WHERE f2.schedDepMin > f1.schedArrMin
RETURN f1.flightId AS inbound, f1.origin AS fromAirport, f2.flightId AS outbound,
       f2.dest AS toAirport, f2.schedDepMin - f1.schedArrMin AS connectMin, count(p) AS pax
ORDER BY pax DESC
LIMIT $limit
"""

TIGHTEST_TURNS = """
MATCH (a:Flight)-[n:NEXT_LEG]->(b:Flight)
RETURN a.tail AS tail, a.flightId AS inbound, b.flightId AS outbound, a.dest AS airport,
       n.turnMin AS turnMin
ORDER BY turnMin, tail
LIMIT $limit
"""

DOWNSTREAM_LEGS = """
MATCH path = (:Flight {flightId: $flightId})-[:NEXT_LEG*1..10]->(d:Flight)
RETURN d.flightId AS flightId, d.origin AS origin, d.dest AS dest, d.schedDep AS dep,
       length(path) AS hops
ORDER BY hops
"""

# --- Disruption impact building blocks ---------------------------------------

FLIGHT_LEG = """
MATCH (f:Flight {flightId: $flightId})
RETURN f.flightId AS flightId, f.legSeq AS legSeq, f.origin AS origin,
       f.dest AS dest, f.schedDepMin AS dep, f.schedArrMin AS arr, f.tail AS tail
"""

# Later legs flown by the same aircraft. Note: within one MATCH pattern openCypher never
# reuses a relationship, so d can never be f here; the disrupted flight comes from FLIGHT_LEG.
AIRCRAFT_RIPPLE = """
MATCH (f:Flight {flightId: $flightId})<-[:OPERATES]-(:Aircraft)-[:OPERATES]->(d:Flight)
WHERE d.legSeq > f.legSeq
RETURN d.flightId AS flightId, d.legSeq AS legSeq, d.origin AS origin,
       d.dest AS dest, d.schedDepMin AS dep, d.schedArrMin AS arr, d.tail AS tail
ORDER BY legSeq
"""

CONNECTIONS_FROM = """
MATCH (p:Passenger)-[:BOOKED_ON]->(f:Flight)-[:ARRIVES_AT]->(a:Airport),
      (p)-[:BOOKED_ON]->(c:Flight)-[:DEPARTS_FROM]->(a)
WHERE f.flightId IN $flightIds AND c.schedDepMin > f.schedArrMin
RETURN f.flightId AS inbound, f.schedArrMin AS inboundArr, f.tail AS inboundTail,
       a.code AS connectAirport, c.flightId AS outbound, c.schedDepMin AS outboundDep,
       c.tail AS outboundTail, c.dest AS finalDest, count(p) AS pax,
       collect(p.passengerId) AS passengerIds,
       sum(CASE WHEN p.tier STARTS WITH 'A-List' THEN 1 ELSE 0 END) AS aListPax
"""

CREW_SEQUENCES = """
MATCH (f:Flight)<-[:ASSIGNED_TO]-(c:Crew)
WHERE f.flightId IN $flightIds
WITH DISTINCT c
MATCH (c)-[a:ASSIGNED_TO]->(g:Flight)
RETURN c.crewId AS crewId, c.name AS name, c.role AS role, c.pairingId AS pairingId,
       c.dutyLimitMin AS dutyLimitMin, a.seq AS seq, g.flightId AS flightId,
       g.origin AS origin, g.dest AS dest, g.schedDepMin AS dep, g.schedArrMin AS arr,
       g.tail AS tail
ORDER BY crewId, seq
"""

REBOOK_DIRECT = """
UNWIND $requests AS r
MATCH (o:Airport)<-[:DEPARTS_FROM]-(f:Flight)-[:ARRIVES_AT]->(d:Airport)
WHERE o.code = r.origin AND d.code = r.dest AND f.schedDepMin >= r.after
  AND f.seatsAvailable > 0
RETURN r.origin AS origin, r.dest AS dest, r.after AS after,
       [f.flightId] AS flights, f.schedDepMin AS dep, f.schedArrMin AS arr,
       f.seatsAvailable AS seats
"""

REBOOK_ONE_STOP = """
UNWIND $requests AS r
MATCH (o:Airport)<-[:DEPARTS_FROM]-(f1:Flight)-[:ARRIVES_AT]->(x:Airport)
      <-[:DEPARTS_FROM]-(f2:Flight)-[:ARRIVES_AT]->(d:Airport)
WHERE o.code = r.origin AND d.code = r.dest AND x.code <> r.dest AND x.code <> r.origin
  AND f1.schedDepMin >= r.after AND f2.schedDepMin >= f1.schedArrMin + $minConnect
  AND f1.seatsAvailable > 0 AND f2.seatsAvailable > 0
RETURN r.origin AS origin, r.dest AS dest, r.after AS after,
       [f1.flightId, f2.flightId] AS flights, f1.schedDepMin AS dep, f2.schedArrMin AS arr,
       CASE WHEN f1.seatsAvailable < f2.seatsAvailable
            THEN f1.seatsAvailable ELSE f2.seatsAvailable END AS seats
"""

# Queries callable directly via {"action": "query", "name": ...}, with default params.
NAMED = {
    "summary": (SUMMARY, {}),
    "routes_from": (ROUTES_FROM, {"airport": "DAL"}),
    "flight": (FLIGHT, {"flightId": None}),
    "aircraft_rotation": (AIRCRAFT_ROTATION, {"tail": None}),
    "hub_connections": (HUB_CONNECTIONS, {"hub": "DAL", "limit": 10}),
    "tightest_turns": (TIGHTEST_TURNS, {"limit": 10}),
    "downstream_legs": (DOWNSTREAM_LEGS, {"flightId": None}),
    "aircraft_ripple": (AIRCRAFT_RIPPLE, {"flightId": None}),
    "rebook_direct": (REBOOK_DIRECT, {"requests": None}),
    "rebook_one_stop": (REBOOK_ONE_STOP, {"requests": None, "minConnect": 40}),
}
