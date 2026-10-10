"""Turn graph query results into a self-contained interactive HTML page (vis-network).

Two views:
  impact   the cascade from one delayed flight: aircraft rotations laid out on a time axis,
           crew pairings and misconnected passenger groups attached to the flights they touch
  network  the route map: airports placed by latitude/longitude, sized by departures

The builders are pure functions (rows in, nodes/edges out) so they can be unit-tested
without AWS. `scripts/graph.py visualize ...` fetches the rows through the graph API Lambda.
"""

from __future__ import annotations

import html
import json

# Extra openCypher used only for drawing (sent through the Lambda's "cypher" action).
FLIGHTS_BY_ID = """
MATCH (f:Flight) WHERE f.flightId IN $ids
RETURN f.flightId AS flightId, f.origin AS origin, f.dest AS dest, f.schedDepMin AS dep,
       f.schedArrMin AS arr, f.tail AS tail, f.legSeq AS legSeq, f.seatsAvailable AS seats
"""

FLIGHTS_BY_TAIL = """
MATCH (a:Aircraft)-[:OPERATES]->(f:Flight) WHERE a.tail IN $tails
RETURN f.flightId AS flightId, f.origin AS origin, f.dest AS dest, f.schedDepMin AS dep,
       f.schedArrMin AS arr, f.tail AS tail, f.legSeq AS legSeq, f.seatsAvailable AS seats
"""

NETWORK_AIRPORTS = """
MATCH (a:Airport)
OPTIONAL MATCH (f:Flight)-[:DEPARTS_FROM]->(a)
RETURN a.code AS code, a.city AS city, a.lat AS lat, a.lon AS lon, a.isHub AS isHub,
       count(f) AS departures
"""

NETWORK_ROUTES = """
MATCH (a:Airport)-[r:ROUTE]->(b:Airport)
WHERE a.code < b.code
RETURN a.code AS a, b.code AS b, r.distanceMi AS distanceMi
"""

PX_PER_MIN = 2.2      # horizontal scale of the time axis
LANE_GAP = 120        # vertical gap between aircraft lanes

COLORS = {
    "disrupted": "#D7263D",
    "aircraft": "#F28C28",
    "crew": "#7B4FD6",
    "normal": "#DCE2EA",
    "misconnect": "#FFFFFF",
    "pax": "#1E5EFF",
    "pairing": "#0B1F3A",
    "aircraft_node": "#5A6578",
    "edge": "#9AA5B5",
}


def hhmm(m: int) -> str:
    day, rem = divmod(int(m), 1440)
    return f"{rem // 60:02d}:{rem % 60:02d}" + (f"+{day}" if day else "")


# --------------------------------------------------------------------------- impact view

def impact_flight_ids(report: dict) -> list[str]:
    """Every flight the report mentions (delayed, crewed by an affected crew, misconnected)."""
    ids = {d["flightId"] for d in report["delayedFlights"]}
    for p in report["crew"]:
        ids.update(l["flightId"] for l in p["legs"])
    for m in report["misconnects"]:
        ids.update((m["inbound"], m["outbound"]))
    return sorted(ids)


def build_impact_graph(report: dict, flights: list[dict]) -> dict:
    """`flights` = rows of FLIGHTS_BY_ID / FLIGHTS_BY_TAIL (duplicates allowed)."""
    by_id = {f["flightId"]: f for f in flights}
    delays = {d["flightId"]: d for d in report["delayedFlights"]}
    origin_id = report["disruption"]["flightId"]
    misconnected_out = {m["outbound"] for m in report["misconnects"]}

    # One lane per aircraft in the cascade (delayed, or worked by an affected crew), the
    # disrupted aircraft first. Other flights (missed connections) share one band below.
    crew_legs = {l["flightId"] for p in report["crew"] for l in p["legs"]}
    in_cascade = lambda f: f["flightId"] in delays or f["flightId"] in crew_legs
    cascade_tails = {f["tail"] for f in by_id.values() if in_cascade(f)}
    cascade_tails.add(report["disruption"]["tail"])
    tails = sorted(cascade_tails,
                   key=lambda t: (t != report["disruption"]["tail"],
                                  min(f["dep"] for f in by_id.values() if f["tail"] == t)))
    lane = {t: i for i, t in enumerate(tails)}
    t0 = min(f["dep"] for f in by_id.values())
    x_of = lambda minutes: round((minutes - t0) * PX_PER_MIN)
    band_y = len(tails) * LANE_GAP + LANE_GAP / 2

    # Stack band flights in rows so boxes never overlap: next free row at that time.
    pos = {}
    for f in by_id.values():
        if f["tail"] in lane:
            pos[f["flightId"]] = (x_of(f["dep"]), lane[f["tail"]] * LANE_GAP)
    row_end = []
    for f in sorted((f for f in by_id.values() if f["tail"] not in lane),
                    key=lambda f: (f["dep"], f["flightId"])):
        x = x_of(f["dep"])
        row = next((i for i, end in enumerate(row_end) if end < x - 190), len(row_end))
        if row == len(row_end):
            row_end.append(x)
        row_end[row] = x
        pos[f["flightId"]] = (x, band_y + row * 64)
    xy = lambda fid: pos.get(fid, (0, 0))

    nodes, edges = [], []
    for t in tails:
        nodes.append({"id": f"ac:{t}", "label": f"{t}", "shape": "box", "fixed": True,
                      "x": x_of(t0) - 170, "y": lane[t] * LANE_GAP,
                      "color": {"background": COLORS["aircraft_node"],
                                "border": COLORS["aircraft_node"]},
                      "font": {"color": "#FFFFFF", "size": 13},
                      "title": f"Aircraft {t}", "group": "aircraft"})
    if row_end:
        nodes.append({"id": "band:missed", "label": "Missed\nconnections", "shape": "box",
                      "fixed": True, "x": x_of(t0) - 170, "y": band_y,
                      "color": {"background": "#FFFFFF", "border": COLORS["disrupted"]},
                      "shapeProperties": {"borderDashes": [4, 3]},
                      "font": {"color": COLORS["disrupted"], "size": 13},
                      "title": "Onward flights that connecting passengers will now miss",
                      "group": "band"})

    for f in sorted(by_id.values(), key=lambda f: (xy(f["flightId"])[1], f["dep"])):
        fid = f["flightId"]
        d = delays.get(fid)
        if fid == origin_id:
            kind = "disrupted"
        elif d and d["source"] == "crew":
            kind = "crew"
        elif d:
            kind = "aircraft"
        elif fid in misconnected_out:
            kind = "misconnect"
        else:
            kind = "normal"
        delay = d["delayMin"] if d else 0
        label = f"{fid}\n{f['origin']}→{f['dest']}\n{hhmm(f['dep'])}" + \
                (f"  +{delay}m" if delay else "")
        title = (f"{fid} {f['origin']}→{f['dest']}\nAircraft {f['tail']}, leg {f['legSeq']}\n"
                 f"Scheduled {hhmm(f['dep'])}–{hhmm(f['arr'])}\n"
                 + (f"Delay {delay} min ({d['source']})\n" if d else "On time\n")
                 + f"Open seats {f.get('seats', '?')}")
        dark = kind in ("disrupted", "aircraft", "crew")
        nodes.append({
            "id": f"fl:{fid}", "label": label, "title": title, "shape": "box",
            "fixed": True, "x": xy(fid)[0], "y": xy(fid)[1], "group": kind,
            "color": {"background": COLORS[kind],
                      "border": COLORS["disrupted"] if kind == "misconnect" else COLORS[kind]},
            "borderWidth": 2 if kind == "misconnect" else 1,
            "shapeProperties": {"borderDashes": [4, 3]} if kind == "misconnect" else {},
            "font": {"color": "#FFFFFF" if dark else "#1A2433", "size": 12, "face": "monospace"},
        })

    # Aircraft rotations: OPERATES into the first leg shown, NEXT_LEG between consecutive legs.
    for t in tails:
        legs = sorted((f for f in by_id.values() if f["tail"] == t), key=lambda f: f["legSeq"])
        if not legs:
            continue
        edges.append({"from": f"ac:{t}", "to": f"fl:{legs[0]['flightId']}",
                      "color": COLORS["edge"], "dashes": True, "arrows": "to",
                      "title": "OPERATES"})
        for a, b in zip(legs, legs[1:]):
            if b["legSeq"] == a["legSeq"] + 1:
                edges.append({"from": f"fl:{a['flightId']}", "to": f"fl:{b['flightId']}",
                              "arrows": "to", "width": 2, "color": COLORS["edge"],
                              "title": f"NEXT_LEG ({b['dep'] - a['arr']} min turn)"})

    # Crew pairings: one node per pairing, linked to every leg it works.
    for p in report["crew"]:
        first = by_id.get(p["legs"][0]["flightId"])
        breach = p["dutyBreach"]
        nodes.append({
            "id": f"cr:{p['pairingId']}",
            "label": f"Crew {p['pairingId']}" + ("\nDUTY BREACH" if breach else ""),
            "title": (f"Crew pairing {p['pairingId']}\n" + "\n".join(p["crew"]) +
                      f"\nProjected duty end {hhmm(p['projectedDutyEndMin'])}"
                      f" (limit {hhmm(p['dutyLimitMin'])})"),
            "shape": "ellipse", "group": "pairing",
            "x": xy(first["flightId"])[0] if first else 0,
            "y": (xy(first["flightId"])[1] if first else 0) - LANE_GAP / 2,
            "color": {"background": COLORS["pairing"],
                      "border": COLORS["disrupted"] if breach else COLORS["pairing"]},
            "borderWidth": 4 if breach else 1,
            "font": {"color": "#FFFFFF", "size": 12},
        })
        for leg in p["legs"]:
            edges.append({"from": f"cr:{p['pairingId']}", "to": f"fl:{leg['flightId']}",
                          "color": COLORS["crew"] if leg["crewInduced"] else "#B7A6E8",
                          "width": 3 if leg["crewInduced"] else 1, "arrows": "to",
                          "title": "ASSIGNED_TO" + (" (crew-induced delay)"
                                                    if leg["crewInduced"] else "")})

    # Misconnected passenger groups: inbound flight -> group -> missed outbound flight.
    for i, m in enumerate(report["misconnects"]):
        gid = f"px:{i}"
        a, b = by_id.get(m["inbound"]), by_id.get(m["outbound"])
        nodes.append({
            "id": gid, "label": f"{m['pax']} pax\n→{m['finalDest']}", "shape": "dot",
            "size": 8 + min(m["pax"], 30), "group": "pax",
            "title": (f"{m['pax']} passengers ({m['aListPax']} A-List)\n"
                      f"{m['inbound']} arrives {m['connectAirport']} {hhmm(m['newInboundArr'])}\n"
                      f"{m['outbound']} to {m['finalDest']} leaves {hhmm(m['newOutboundDep'])}"
                      f" (slack {m['slackMin']} min)"),
            "x": round((xy(m["inbound"])[0] + xy(m["outbound"])[0]) / 2),
            "y": (xy(m["inbound"])[1] + xy(m["outbound"])[1]) / 2,
            "color": {"background": COLORS["pax"], "border": COLORS["pax"]},
            "font": {"color": "#1A2433", "size": 11},
        })
        edges.append({"from": f"fl:{m['inbound']}", "to": gid, "color": "#8FB0FF",
                      "arrows": "to", "title": "BOOKED_ON (inbound)"})
        edges.append({"from": gid, "to": f"fl:{m['outbound']}", "color": COLORS["disrupted"],
                      "dashes": [2, 4], "arrows": "to", "title": "BOOKED_ON (missed)"})

    s, d = report["summary"], report["disruption"]
    return {
        "title": f"{d['flightId']} {d['route']} delayed {d['delayMin']} minutes",
        "subtitle": "Each row is one aircraft's day, left to right by scheduled departure; "
                    "missed connecting flights sit in the band below. "
                    "Drag to pan, scroll to zoom, hover for details.",
        "stats": [
            (s["flightsDelayed"], "flights delayed"),
            (len(s["crewInducedFlights"]), "delayed by crews changing aircraft"),
            (s["crewDutyBreaches"], "crew duty-limit breaches"),
            (s["misconnectedPassengers"], "passengers misconnected"),
        ],
        "legend": [
            (COLORS["disrupted"], "Disrupted flight"),
            (COLORS["aircraft"], "Delayed: same aircraft"),
            (COLORS["crew"], "Delayed: crew changed aircraft"),
            (COLORS["normal"], "Not delayed"),
            ("dash", "Missed by connecting passengers"),
            (COLORS["pairing"], "Crew pairing (red ring = duty breach)"),
            (COLORS["pax"], "Misconnected passenger group"),
        ],
        "nodes": nodes, "edges": edges, "physics": True,
    }


def fetch_impact(invoke, flight_id: str, delay_min: int) -> tuple[dict, dict]:
    """Run the impact analysis and fetch what the drawing needs, through `invoke`
    (a function taking a graph-API payload and returning its result). Returns
    (impact report, graph for render_html)."""
    report = invoke({"action": "impact", "flightId": flight_id, "delayMin": int(delay_min)})
    ids = impact_flight_ids(report)
    flights = invoke({"action": "cypher", "query": FLIGHTS_BY_ID, "params": {"ids": ids}})
    delayed = {d["flightId"] for d in report["delayedFlights"]}
    tails = sorted({f["tail"] for f in flights if f["flightId"] in delayed})
    flights += invoke({"action": "cypher", "query": FLIGHTS_BY_TAIL, "params": {"tails": tails}})
    return report, build_impact_graph(report, flights)


# --------------------------------------------------------------------------- network view

def build_network_graph(airports: list[dict], routes: list[dict]) -> dict:
    lats = [a["lat"] for a in airports]
    lons = [a["lon"] for a in airports]
    width, height = 1400, 800
    sx = width / (max(lons) - min(lons))
    sy = height / (max(lats) - min(lats))
    scale = min(sx, sy)
    hub = {a["code"] for a in airports if a.get("isHub") in (True, "true")}
    nodes = []
    for a in airports:
        is_hub = a["code"] in hub
        nodes.append({
            "id": a["code"], "label": a["code"], "shape": "dot", "fixed": True,
            "x": round((a["lon"] - min(lons)) * scale),
            "y": round((max(lats) - a["lat"]) * scale),
            "size": 10 + a["departures"] * (0.35 if is_hub else 1.2),
            "title": f"{a['code']} · {a['city']}\n{a['departures']} departures",
            "color": {"background": COLORS["disrupted"] if is_hub else COLORS["pairing"],
                      "border": "#FFFFFF"},
            "font": {"size": 16, "color": "#1A2433", "strokeWidth": 4,
                     "strokeColor": "#FFFFFF"},
            "group": "hub" if is_hub else "airport",
        })
    edges = []
    for r in routes:
        spoke = r["a"] in hub or r["b"] in hub
        edges.append({"from": r["a"], "to": r["b"], "title": f"{r['a']}–{r['b']}, "
                      f"{r['distanceMi']} mi", "width": 1.5 if spoke else 1,
                      "color": {"color": "#F4A3AE" if spoke else "#9AA5B5",
                                "opacity": 0.8}})
    total = sum(a["departures"] for a in airports)
    hub_dep = sum(a["departures"] for a in airports if a["code"] in hub)
    return {
        "title": "Route network",
        "subtitle": "Airports placed by real latitude and longitude, sized by daily departures. "
                    "Hover for details.",
        "stats": [(len(airports), "airports"), (len(routes), "routes"),
                  (total, "daily departures"), (hub_dep, "departures from the hub")],
        "legend": [(COLORS["disrupted"], "Hub (DAL) and its routes"),
                   (COLORS["pairing"], "Airport"), ("#9AA5B5", "Point-to-point route")],
        "nodes": nodes, "edges": edges, "physics": False,
    }


# --------------------------------------------------------------------------- HTML

VIS_URL = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"


def render_html(graph: dict, embed: bool = False) -> str:
    """Standalone page, or with embed=True just the legend and the drawing (for the web UI,
    which shows the title and numbers itself)."""
    data = json.dumps({"nodes": graph["nodes"], "edges": graph["edges"],
                       "physics": graph["physics"]}).replace("</", "<\\/")
    stats = "".join(f'<div class="stat"><b>{html.escape(str(v))}</b>'
                    f'<span>{html.escape(t)}</span></div>' for v, t in graph["stats"])
    legend = "".join(
        (f'<span class="key"><i class="dash"></i>{html.escape(t)}</span>' if c == "dash" else
         f'<span class="key"><i style="background:{c}"></i>{html.escape(t)}</span>')
        for c, t in graph["legend"])
    header = (f'<header class="embed"><div class="legend">{legend}</div></header>' if embed else
              f'<header><h1>{html.escape(graph["title"])}</h1>'
              f'<p>{html.escape(graph["subtitle"])}</p>'
              f'<div class="stats">{stats}</div><div class="legend">{legend}</div></header>')
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(graph["title"])}</title>
<script src="{VIS_URL}"></script>
<style>
  :root {{ --ink:#1A2433; --muted:#5A6578; --rule:#CBD2DC; --navy:#0B1F3A; --bg:#FFFFFF; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font-family:"IBM Plex Sans","Helvetica Neue",Helvetica,sans-serif;
         display:flex; flex-direction:column; height:100vh; }}
  header {{ padding:20px 28px 12px; border-top:6px solid var(--navy); }}
  header.embed {{ padding:6px 8px 8px; border-top:0; }}
  header.embed .legend {{ margin-top:0; font-size:12px; }}
  h1 {{ margin:0 0 4px; font-size:24px; color:var(--navy); }}
  p {{ margin:0; color:var(--muted); font-size:14px; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:28px; margin-top:14px; }}
  .stat b {{ display:block; font-size:28px; color:var(--navy); line-height:1.1; }}
  .stat span {{ font-size:13px; color:var(--muted); }}
  .legend {{ display:flex; flex-wrap:wrap; gap:8px 18px; margin-top:12px; font-size:13px; }}
  .key {{ display:inline-flex; align-items:center; gap:6px; }}
  .key i {{ width:14px; height:14px; border-radius:3px; border:1px solid var(--rule); }}
  .key i.dash {{ border:2px dashed #D7263D; background:#fff; }}
  #graph {{ flex:1; border-top:1px solid var(--rule); min-height:400px; }}
  #err {{ display:none; padding:16px 28px; color:#D7263D; }}
</style>
</head>
<body>
{header}
<div id="err">Could not load the drawing library from unpkg.com. Check your internet connection.</div>
<div id="graph"></div>
<script>
  const DATA = {data};
  if (typeof vis === "undefined") {{
    document.getElementById("err").style.display = "block";
  }} else {{
    const options = {{
      interaction: {{ hover: true, tooltipDelay: 120, navigationButtons: true }},
      edges: {{ smooth: {{ type: "continuous" }}, arrows: {{ to: {{ scaleFactor: 0.5 }} }} }},
      nodes: {{ margin: 8, widthConstraint: {{ maximum: 170 }} }},
      physics: DATA.physics ? {{
        solver: "forceAtlas2Based",
        forceAtlas2Based: {{ gravitationalConstant: -40, springLength: 90 }},
        stabilization: {{ iterations: 400 }}
      }} : false
    }};
    const network = new vis.Network(
      document.getElementById("graph"),
      {{ nodes: new vis.DataSet(DATA.nodes), edges: new vis.DataSet(DATA.edges) }},
      options);
    network.once("stabilizationIterationsDone", () => network.setOptions({{ physics: false }}));
    network.once("afterDrawing", () => network.fit({{ animation: false }}));
  }}
</script>
</body>
</html>
"""
