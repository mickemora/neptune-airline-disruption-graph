"""Source-system files: formats, deliberate quirks, and that they still describe the same day.

These checks are pure Python; test_lakehouse.py runs the real Spark transformations.
"""

import csv
import json

from datagen.business_rules import LOYALTY_TIERS, int_rule, rules_dict
from datagen.generate import SERVICE_DATE


def _csv(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_every_source_folder_has_its_files(sources):
    manifest, root = sources
    for rel, rows in manifest["files"].items():
        assert (root / rel).is_file(), rel
        assert rows > 0, rel
    assert manifest["serviceDate"] == SERVICE_DATE


def test_schedule_is_ssim_style(sources, dataset):
    gen, _ = dataset
    _, root = sources
    rows = _csv(next((root / "scheduling").glob("*.csv")))
    assert len(rows) == len(gen.flights) + 2               # + 2 corrupt rows
    for r in rows:
        assert len(r["FLT_NO"]) == 4 and r["FLT_NO"].isdigit()
        assert len(r["STD"]) == 4 and r["SVC_DATE"] == SERVICE_DATE.replace("-", "")
    by_id = {f"WN{int(r['FLT_NO'])}": r for r in rows}
    for f in gen.flights:
        r = by_id[f.flight_id]
        assert int(r["STD"][:2]) * 60 + int(r["STD"][2:]) == f.dep
        assert (int(r["STA"][:2]) * 60 + int(r["STA"][2:])
                + 1440 * int(r["STA_DAY_OFFSET"])) == f.arr


def test_crew_pairings_carry_no_duty_limits(sources, dataset):
    """Duty limits are derived from the governed rules, not shipped by the crew system."""
    gen, _ = dataset
    _, root = sources
    pairings = _jsonl(next((root / "crew").glob("*.json")))
    assert len(pairings) == len(gen.pairings)
    assert sum(len(p["crew"]) for p in pairings) == len(gen.crew)
    assert {c["position"] for p in pairings for c in p["crew"]} == {"CA", "FO", "FA"}
    assert not any("duty" in k for p in pairings for k in p)


def test_replaying_the_cdc_feed_gives_the_booked_segments(sources, dataset):
    """Reference implementation of the silver CDC logic, in plain Python."""
    gen, _ = dataset
    manifest, root = sources
    seen, latest = set(), {}
    for path in sorted((root / "reservations").glob("*.csv")):
        for r in _csv(path):
            key = tuple(r.values())
            if key in seen:
                continue                                    # re-delivered duplicate
            seen.add(key)
            seg = (r["RECORD_LOCATOR"], r["SEGMENT_NO"])
            if seg not in latest or int(r["CHANGE_SEQ"]) > int(latest[seg]["CHANGE_SEQ"]):
                latest[seg] = r
    flights = {f.flight_id for f in gen.flights}
    active = {(r["RECORD_LOCATOR"], int(r["SEGMENT_NO"]), r["CUSTOMER_ID"], r["FLIGHT"])
              for r in latest.values()
              if r["OP"] != "D" and r["SEG_STATUS"] == "HK" and r["FLIGHT"] in flights}
    expected = {(b["pnr"], b["segment"], b["pax"], b["flight"]) for b in gen.bookings}
    assert active == expected
    a = manifest["anomalies"]
    assert a["movedSegments"] > 0 and a["duplicateCdcRows"] == 100
    assert a["cancelledBookings"] + a["deletedBookings"] == 150


def test_loyalty_has_members_only_and_latest_record_wins(sources, dataset):
    gen, _ = dataset
    _, root = sources
    names = {code: name for code, name, *_ in LOYALTY_TIERS}
    latest = {}
    for m in _jsonl(next((root / "loyalty").glob("*.json"))):
        if m["customer_id"] not in latest or m["updated_at"] > latest[m["customer_id"]]["updated_at"]:
            latest[m["customer_id"]] = m
    for p in gen.passengers:
        if p["tier"] == "None":
            assert p["id"] not in latest
        else:
            assert names[latest[p["id"]]["tier_code"].upper()] == p["tier"]


def test_ops_events_last_estimate_wins(sources, dataset):
    gen, _ = dataset
    manifest, root = sources
    events = _jsonl(next((root / "ops_events").glob("*.json")))
    dep = {f.flight_id: f.dep for f in gen.flights}
    last = {}
    for e in sorted(events, key=lambda e: e["event_ts"]):
        last[e["flight"]] = int(e["etd"][:2]) * 60 + int(e["etd"][2:]) - dep[e["flight"]]
    assert last == manifest["expectedDelays"]
    assert "WN4608" in last


def test_generator_and_impact_defaults_follow_the_business_rules():
    import impact
    from datagen import generate
    from datagen.airports import HUB
    assert rules_dict()["HUB_AIRPORT"] == HUB
    assert generate.MIN_TURN == impact.MIN_TURN == int_rule("MIN_TURN_MIN")
    assert impact.MIN_CONNECT == int_rule("MIN_CONNECT_MIN")
    assert impact.CREW_MIN_CONNECT == int_rule("CREW_MIN_CONNECT_MIN")
    assert impact.CREW_DEBRIEF == generate.CREW_DEBRIEF == int_rule("CREW_DEBRIEF_MIN")
    assert generate.MAX_DUTY == int_rule("MAX_DUTY_MIN")
