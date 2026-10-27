"""Every transformation in the lakehouse, as plain PySpark functions.

The Lakeflow pipeline (transformations/*.py) only wires these into streaming tables and
materialized views; the same functions run in the local test suite against the same
source files, so the logic is tested without a Databricks workspace.

Layers
  bronze  raw records exactly as the source systems sent them (+ file and load time)
  silver  cleaned and conformed: one row per business key, typed, decoded, validated
  gold    the graph projection (node_* / edge_* tables, shaped by the ontology) and two
          analytics tables (connections, crew_duty) for the semantic layer

Inputs are referenced as "<layer>.<table>"; "semantic.*" tables are governed reference
data maintained outside the pipeline (business rules, loyalty tiers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

# ============================================================================ sources

@dataclass(frozen=True)
class Source:
    table: str      # bronze table name
    folder: str     # folder under the landing volume
    format: str     # csv | json
    schema: T.StructType
    comment: str


def _strings(*cols: str) -> T.StructType:
    return T.StructType([T.StructField(c, T.StringType()) for c in cols])


SOURCES = [
    Source("airports", "reference", "csv",
           _strings("IATA", "NAME", "CITY", "STATE", "LAT", "LON"),
           "Reference data: airports with IATA codes and coordinates."),
    Source("routes", "network", "csv", _strings("ORIG", "DEST", "DIST_MI"),
           "Network planning: authorized city pairs, one row per direction."),
    Source("aircraft_master", "fleet", "csv",
           _strings("TAIL_NUMBER", "AIRCRAFT_TYPE", "SEAT_CONFIG", "HOME_BASE", "STATUS"),
           "Maintenance & engineering fleet master, including inactive tails."),
    Source("schedule", "scheduling", "csv",
           _strings("CARRIER", "FLT_NO", "SVC_DATE", "ORIG", "DEST", "STD", "STA",
                    "STA_DAY_OFFSET", "EQUIP", "TAIL", "LEG_SEQ", "DIST_MI"),
           "Schedule extract (SSIM style): zero-padded flight numbers, HHMM times."),
    Source("crew_pairings", "crew", "json", T.StructType([
        T.StructField("pairing_id", T.StringType()),
        T.StructField("service_date", T.StringType()),
        T.StructField("legs", T.ArrayType(T.StructType([
            T.StructField("seq", T.LongType()), T.StructField("flight", T.StringType())]))),
        T.StructField("crew", T.ArrayType(T.StructType([
            T.StructField("employee_id", T.StringType()),
            T.StructField("name", T.StringType()),
            T.StructField("position", T.StringType()),
            T.StructField("base", T.StringType())]))),
    ]), "Crew management system: one nested record per pairing."),
    Source("reservations_cdc", "reservations", "csv",
           _strings("OP", "CHANGE_SEQ", "CHANGED_AT", "RECORD_LOCATOR", "SEGMENT_NO",
                    "CUSTOMER_ID", "PAX_NAME", "FLIGHT", "SVC_DATE", "SEG_STATUS"),
           "Reservations change data capture: inserts, updates and deletes per segment."),
    Source("loyalty_members", "loyalty", "json",
           _strings("customer_id", "tier_code", "enrolled_on", "updated_at"),
           "Loyalty platform: member profiles with history. Non-members are absent."),
    Source("ops_events", "ops_events", "json",
           _strings("event_id", "event_type", "flight", "svc_date", "etd", "reason",
                    "event_ts"),
           "Ops control event stream: estimated departure updates."),
]

RULES_SCHEMA = T.StructType([
    T.StructField("rule_id", T.StringType(), False),
    T.StructField("value", T.StringType(), False),
    T.StructField("unit", T.StringType()),
    T.StructField("description", T.StringType()),
    T.StructField("owner", T.StringType()),
])
TIERS_SCHEMA = T.StructType([
    T.StructField("tier_code", T.StringType(), False),
    T.StructField("tier_name", T.StringType(), False),
    T.StructField("priority_rank", T.IntegerType()),
    T.StructField("is_priority", T.BooleanType()),
])
INT_RULES = ["MIN_CONNECT_MIN", "MIN_TURN_MIN", "CREW_MIN_CONNECT_MIN", "CREW_REPORT_MIN",
             "CREW_DEBRIEF_MIN", "MAX_DUTY_MIN"]
POSITIONS = {"CA": "Captain", "FO": "First Officer", "FA": "Flight Attendant"}


def add_ingest_metadata(df: DataFrame) -> DataFrame:
    return df.select("*", F.col("_metadata.file_path").alias("_source_file"),
                     F.current_timestamp().alias("_ingested_at"))


# ============================================================================ helpers

def latest(df: DataFrame, keys: list[str], *order: Column) -> DataFrame:
    """Keep the newest row per business key (ties: the later file, then load time)."""
    w = Window.partitionBy(*keys).orderBy(*order, F.col("_source_file").desc(),
                                          F.col("_ingested_at").desc())
    return df.withColumn("_rn", F.row_number().over(w)).filter("_rn = 1").drop("_rn")


def hhmm_to_min(c: Column) -> Column:
    """'0605' -> 365; anything that isn't a valid 24h HHMM -> NULL."""
    return F.when(c.rlike("^([01][0-9]|2[0-3])[0-5][0-9]$"),
                  F.substring(c, 1, 2).cast("int") * 60 + F.substring(c, 3, 2).cast("int"))


def min_to_hhmm(c: Column) -> Column:
    """365 -> '06:05'; 1470 -> '24:30' (arrivals after midnight keep counting)."""
    return F.concat(F.lpad(F.floor(c / 60).cast("string"), 2, "0"), F.lit(":"),
                    F.lpad((c % 60).cast("string"), 2, "0"))


def rule_values(rules: DataFrame) -> DataFrame:
    """One-row DataFrame: HUB_AIRPORT plus every integer rule as an INT column."""
    wide = rules.groupBy().pivot("rule_id", ["HUB_AIRPORT"] + INT_RULES).agg(F.first("value"))
    return wide.select(F.col("HUB_AIRPORT"), *[F.col(r).cast("int").alias(r) for r in INT_RULES])


def node_id(prefix: str, *cols) -> Column:
    return F.concat(F.lit(prefix), *[F.col(c) if isinstance(c, str) else c for c in cols])


# ============================================================================ silver

def silver_airports(airports: DataFrame, rules: DataFrame) -> DataFrame:
    a = latest(airports.withColumn("IATA", F.upper(F.trim("IATA"))), ["IATA"])
    return a.crossJoin(rule_values(rules).select("HUB_AIRPORT")).select(
        F.col("IATA").alias("code"), F.trim("NAME").alias("name"),
        F.trim("CITY").alias("city"), F.trim("STATE").alias("state"),
        F.col("LAT").cast("double").alias("lat"), F.col("LON").cast("double").alias("lon"),
        (F.col("IATA") == F.col("HUB_AIRPORT")).alias("is_hub"))


def silver_routes(routes: DataFrame) -> DataFrame:
    r = latest(routes, ["ORIG", "DEST"])
    return r.select(F.upper("ORIG").alias("origin"), F.upper("DEST").alias("dest"),
                    F.col("DIST_MI").cast("int").alias("distance_mi"))


def silver_aircraft(fleet: DataFrame) -> DataFrame:
    a = latest(fleet, ["TAIL_NUMBER"]).filter(F.upper("STATUS") == "ACTIVE")
    return a.select(
        F.col("TAIL_NUMBER").alias("tail"), F.col("AIRCRAFT_TYPE").alias("aircraft_type"),
        F.regexp_extract("SEAT_CONFIG", r"(\d+)", 1).cast("int").alias("seats"),
        F.col("HOME_BASE").alias("home_base"))


def silver_flights(schedule: DataFrame) -> DataFrame:
    s = latest(schedule, ["CARRIER", "FLT_NO", "SVC_DATE"])
    return s.select(
        F.concat(F.col("CARRIER"), F.col("FLT_NO").cast("int").cast("string")).alias("flight_id"),
        F.to_date("SVC_DATE", "yyyyMMdd").alias("service_date"),
        F.col("ORIG").alias("origin"), F.col("DEST").alias("dest"),
        hhmm_to_min(F.col("STD")).alias("sched_dep_min"),
        (hhmm_to_min(F.col("STA")) + 1440 * F.col("STA_DAY_OFFSET").cast("int"))
        .alias("sched_arr_min"),
        F.col("DIST_MI").cast("int").alias("distance_mi"),
        F.col("TAIL").alias("tail"), F.col("LEG_SEQ").cast("int").alias("leg_seq"),
        F.col("EQUIP").alias("equipment"))


def _pairings(pairings: DataFrame) -> DataFrame:
    return latest(pairings, ["pairing_id"])


def silver_crew_pairings(pairings: DataFrame) -> DataFrame:
    return _pairings(pairings).select(
        "pairing_id", F.to_date("service_date").alias("service_date"),
        F.size("legs").alias("leg_count"), F.size("crew").alias("crew_count"))


def silver_crew_legs(pairings: DataFrame) -> DataFrame:
    return (_pairings(pairings).select("pairing_id", F.explode("legs").alias("leg"))
            .select("pairing_id", F.col("leg.seq").cast("int").alias("seq"),
                    F.col("leg.flight").alias("flight_id")))


def silver_crew_members(pairings: DataFrame) -> DataFrame:
    code = F.upper(F.col("c.position"))
    role = F.lit(None).cast("string")
    for k, v in reversed(list(POSITIONS.items())):   # CASE WHEN ... (NULL if unknown)
        role = F.when(code == k, v).otherwise(role)
    return (_pairings(pairings).select("pairing_id", F.explode("crew").alias("c"))
            .select(F.col("c.employee_id").alias("crew_id"), F.col("c.name").alias("name"),
                    role.alias("role"),
                    F.col("c.base").alias("base"), "pairing_id"))


def silver_bookings(cdc: DataFrame, flights: DataFrame) -> DataFrame:
    """Apply the CDC feed: latest change per segment wins; deletes and cancels drop out."""
    business = [c for c in cdc.columns if not c.startswith("_")]
    changes = cdc.dropDuplicates(business)   # the extract re-sends rows now and then
    current = latest(changes, ["RECORD_LOCATOR", "SEGMENT_NO"],
                     F.col("CHANGE_SEQ").cast("long").desc())
    active = current.filter((F.col("OP") != "D") & (F.col("SEG_STATUS") == "HK"))
    scheduled = flights.select("flight_id", "service_date", F.lit(True).alias("on_schedule"))
    return (active.select(
        F.col("RECORD_LOCATOR").alias("record_locator"),
        F.col("SEGMENT_NO").cast("int").alias("segment_no"),
        F.col("CUSTOMER_ID").alias("customer_id"), F.col("PAX_NAME").alias("pax_name"),
        F.col("FLIGHT").alias("flight_id"), F.to_date("SVC_DATE").alias("service_date"),
        F.col("CHANGE_SEQ").cast("long").alias("last_change_seq"),
        F.to_timestamp("CHANGED_AT").alias("last_changed_at"))
        .join(scheduled, ["flight_id", "service_date"], "left")
        .withColumn("on_schedule", F.coalesce("on_schedule", F.lit(False))))


def silver_passengers(bookings: DataFrame, members: DataFrame, tiers: DataFrame) -> DataFrame:
    """Customers with a confirmed segment, named as booked, tiered by the loyalty system."""
    pax = bookings.groupBy("customer_id").agg(F.min("pax_name").alias("pax_name"))
    parts = F.split("pax_name", "/")
    named = pax.select("customer_id", F.concat_ws(" ", F.initcap(parts[1]),
                                                  F.initcap(parts[0])).alias("name"))
    profile = latest(members.withColumn("tier_code", F.upper(F.trim("tier_code"))),
                     ["customer_id"], F.to_timestamp("updated_at").desc())
    return (named.join(profile.select("customer_id", "tier_code"), "customer_id", "left")
            .withColumn("tier_code", F.coalesce("tier_code", F.lit("NONE")))
            .join(tiers.select("tier_code", "tier_name", "is_priority"), "tier_code", "left")
            .select(F.col("customer_id").alias("passenger_id"), "name",
                    F.col("tier_name").alias("tier"), "tier_code", "is_priority"))


def silver_flight_status(events: DataFrame, flights: DataFrame) -> DataFrame:
    """Latest estimated departure per flight; the delay is measured against the schedule."""
    etd = latest(events.filter(F.col("event_type") == "ETD_UPDATE"), ["flight", "svc_date"],
                 F.to_timestamp("event_ts").desc())
    return (etd.select(F.col("flight").alias("flight_id"),
                       F.to_date("svc_date").alias("service_date"),
                       hhmm_to_min(F.col("etd")).alias("est_dep_min"),
                       F.col("reason").alias("delay_reason"),
                       F.to_timestamp("event_ts").alias("updated_at"))
            .join(flights.select("flight_id", "service_date", "sched_dep_min"),
                  ["flight_id", "service_date"])
            .withColumn("delay_min", F.col("est_dep_min") - F.col("sched_dep_min"))
            .drop("sched_dep_min"))


# ============================================================================ gold: graph

def gold_node_airport(airports: DataFrame) -> DataFrame:
    return airports.select(node_id("AP-", "code").alias("node_id"), "*")


def gold_node_aircraft(aircraft: DataFrame) -> DataFrame:
    return aircraft.select(node_id("AC-", "tail").alias("node_id"), "tail", "aircraft_type",
                           "seats", F.col("home_base").alias("base"))


def gold_node_flight(flights: DataFrame, aircraft: DataFrame, bookings: DataFrame,
                     status: DataFrame) -> DataFrame:
    booked = bookings.groupBy("flight_id", "service_date").agg(F.count("*").alias("booked"))
    delay = F.greatest(F.coalesce("delay_min", F.lit(0)), F.lit(0))
    f = (flights.join(aircraft.select("tail", F.col("seats").alias("capacity")), "tail")
         .join(booked, ["flight_id", "service_date"], "left")
         .join(status.select("flight_id", "service_date", "delay_min"),
               ["flight_id", "service_date"], "left")
         .withColumn("booked", F.coalesce("booked", F.lit(0))))
    return f.select(
        node_id("FL-", "flight_id").alias("node_id"), "flight_id",
        F.date_format("service_date", "yyyy-MM-dd").alias("service_date"), "origin", "dest",
        "sched_dep_min", "sched_arr_min", min_to_hhmm(F.col("sched_dep_min")).alias("sched_dep"),
        min_to_hhmm(F.col("sched_arr_min")).alias("sched_arr"), "distance_mi", "tail",
        "leg_seq", "capacity", "booked",
        (F.col("capacity") - F.col("booked")).alias("seats_available"),
        F.when(delay > 0, "DELAYED").otherwise("SCHEDULED").alias("status"),
        delay.alias("est_delay_min"))


def _duty(legs: DataFrame, flights: DataFrame, rules: DataFrame) -> DataFrame:
    """Per pairing: duty start/limit from the rules, scheduled release, aircraft changes."""
    lf = legs.join(flights.select("flight_id", "sched_dep_min", "sched_arr_min", "tail"),
                   "flight_id")
    w = Window.partitionBy("pairing_id").orderBy("seq")
    lf = lf.withColumn("changes_aircraft",
                       (F.lag("tail").over(w).isNotNull() & (F.lag("tail").over(w) != F.col("tail")))
                       .cast("int"))
    r = rule_values(rules)
    return (lf.groupBy("pairing_id").agg(
        F.min_by("sched_dep_min", "seq").alias("first_dep_min"),
        F.max_by("sched_arr_min", "seq").alias("last_arr_min"),
        F.count("*").alias("leg_count"), F.sum("changes_aircraft").alias("aircraft_changes"))
        .crossJoin(r)
        .select("pairing_id", "leg_count", "aircraft_changes",
                (F.col("first_dep_min") - F.col("CREW_REPORT_MIN")).alias("duty_start_min"),
                (F.col("last_arr_min") + F.col("CREW_DEBRIEF_MIN")).alias("sched_release_min"),
                (F.col("first_dep_min") - F.col("CREW_REPORT_MIN") + F.col("MAX_DUTY_MIN"))
                .alias("duty_limit_min")))


def gold_node_crew(members: DataFrame, legs: DataFrame, flights: DataFrame,
                   rules: DataFrame) -> DataFrame:
    duty = _duty(legs, flights, rules).select("pairing_id", "duty_start_min", "duty_limit_min")
    return members.join(duty, "pairing_id").select(
        node_id("CR-", "crew_id").alias("node_id"), "crew_id", "name", "role", "base",
        "pairing_id", "duty_start_min", "duty_limit_min")


def gold_node_passenger(passengers: DataFrame) -> DataFrame:
    return passengers.select(node_id("PX-", "passenger_id").alias("node_id"), "passenger_id",
                             "name", "tier")


def gold_node_business_rule(rules: DataFrame) -> DataFrame:
    return rules.select(node_id("BR-", "rule_id").alias("node_id"), "rule_id", "value", "unit",
                        "description")


def gold_edge_route(routes: DataFrame) -> DataFrame:
    return routes.select(node_id("RT-", "origin", F.lit("-"), "dest").alias("edge_id"),
                         node_id("AP-", "origin").alias("start_id"),
                         node_id("AP-", "dest").alias("end_id"), "distance_mi")


def gold_edge_departs_from(flights: DataFrame) -> DataFrame:
    return flights.select(node_id("DF-", "flight_id").alias("edge_id"),
                          node_id("FL-", "flight_id").alias("start_id"),
                          node_id("AP-", "origin").alias("end_id"))


def gold_edge_arrives_at(flights: DataFrame) -> DataFrame:
    return flights.select(node_id("AA-", "flight_id").alias("edge_id"),
                          node_id("FL-", "flight_id").alias("start_id"),
                          node_id("AP-", "dest").alias("end_id"))


def gold_edge_operates(flights: DataFrame) -> DataFrame:
    return flights.select(node_id("OP-", "flight_id").alias("edge_id"),
                          node_id("AC-", "tail").alias("start_id"),
                          node_id("FL-", "flight_id").alias("end_id"), "leg_seq")


def gold_edge_next_leg(flights: DataFrame) -> DataFrame:
    a, b = flights.alias("a"), flights.alias("b")
    pairs = a.join(b, (F.col("a.tail") == F.col("b.tail"))
                   & (F.col("a.service_date") == F.col("b.service_date"))
                   & (F.col("b.leg_seq") == F.col("a.leg_seq") + 1))
    return pairs.select(F.concat(F.lit("NL-"), F.col("a.flight_id")).alias("edge_id"),
                        F.concat(F.lit("FL-"), F.col("a.flight_id")).alias("start_id"),
                        F.concat(F.lit("FL-"), F.col("b.flight_id")).alias("end_id"),
                        (F.col("b.sched_dep_min") - F.col("a.sched_arr_min")).alias("turn_min"))


def gold_edge_assigned_to(members: DataFrame, legs: DataFrame) -> DataFrame:
    return members.join(legs, "pairing_id").select(
        node_id("AS-", "crew_id", F.lit("-"), F.col("seq").cast("string")).alias("edge_id"),
        node_id("CR-", "crew_id").alias("start_id"), node_id("FL-", "flight_id").alias("end_id"),
        "seq", "role")


def gold_edge_booked_on(bookings: DataFrame) -> DataFrame:
    return bookings.select(
        node_id("BK-", "record_locator", F.lit("-"), F.col("segment_no").cast("string"))
        .alias("edge_id"),
        node_id("PX-", "customer_id").alias("start_id"),
        node_id("FL-", "flight_id").alias("end_id"), "record_locator", "segment_no")


# ============================================================================ gold: analytics

def gold_connections(bookings: DataFrame, flights: DataFrame, passengers: DataFrame,
                     rules: DataFrame) -> DataFrame:
    """One row per connecting itinerary, with its slack against the minimum connect time."""
    s1 = bookings.filter("segment_no = 1").select("record_locator", "customer_id",
                                                  F.col("flight_id").alias("inbound_flight"))
    s2 = bookings.filter("segment_no = 2").select("record_locator",
                                                  F.col("flight_id").alias("outbound_flight"))
    f1 = flights.select(F.col("flight_id").alias("inbound_flight"),
                        F.col("dest").alias("connect_airport"),
                        F.col("sched_arr_min").alias("inbound_arr_min"),
                        F.col("tail").alias("inbound_tail"), "service_date")
    f2 = flights.select(F.col("flight_id").alias("outbound_flight"),
                        F.col("dest").alias("final_dest"),
                        F.col("sched_dep_min").alias("outbound_dep_min"),
                        F.col("tail").alias("outbound_tail"))
    mct = rule_values(rules).select(F.col("MIN_CONNECT_MIN").alias("min_connect_min"))
    c = (s1.join(s2, "record_locator").join(f1, "inbound_flight").join(f2, "outbound_flight")
         .join(passengers.select(F.col("passenger_id").alias("customer_id"), "tier",
                                 "is_priority"), "customer_id")
         .crossJoin(mct))
    connect = F.col("outbound_dep_min") - F.col("inbound_arr_min")
    return c.select(
        "record_locator", F.col("customer_id").alias("passenger_id"), "tier", "is_priority",
        "service_date", "inbound_flight", "outbound_flight", "connect_airport", "final_dest",
        "inbound_arr_min", "outbound_dep_min", connect.alias("connect_min"), "min_connect_min",
        (connect - F.col("min_connect_min")).alias("slack_min"),
        (F.col("inbound_tail") == F.col("outbound_tail")).alias("same_aircraft"))


def gold_crew_duty(members: DataFrame, legs: DataFrame, flights: DataFrame,
                   rules: DataFrame) -> DataFrame:
    """One row per pairing: how close the scheduled day already runs to the duty limit."""
    crew = members.groupBy("pairing_id").agg(
        F.count("*").alias("crew_count"),
        F.max(F.when(F.col("role") == "Captain", F.col("base"))).alias("base"))
    return (_duty(legs, flights, rules).join(crew, "pairing_id")
            .withColumn("buffer_min", F.col("duty_limit_min") - F.col("sched_release_min")))


# ============================================================================ registry

@dataclass(frozen=True)
class Spec:
    layer: str
    name: str
    build: Callable[..., DataFrame]
    inputs: tuple[str, ...]
    comment: str = ""
    expectations: dict[str, str] = field(default_factory=dict)


SILVER = [
    Spec("silver", "airports", silver_airports, ("bronze.airports", "semantic.business_rules"),
         "Airports, one row per IATA code; is_hub follows the HUB_AIRPORT rule.",
         {"valid_iata_code": "code RLIKE '^[A-Z]{3}$'",
          "valid_coordinates": "lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180"}),
    Spec("silver", "routes", silver_routes, ("bronze.routes",),
         "Authorized city pairs, one row per direction.",
         {"origin_differs_from_destination": "origin <> dest"}),
    Spec("silver", "aircraft", silver_aircraft, ("bronze.aircraft_master",),
         "Active aircraft with seat counts parsed from the cabin configuration.",
         {"has_seat_count": "seats > 0"}),
    Spec("silver", "flights", silver_flights, ("bronze.schedule",),
         "Scheduled legs with decoded flight numbers and times as minutes after midnight.",
         {"valid_departure_time": "sched_dep_min IS NOT NULL",
          "valid_arrival_time": "sched_arr_min IS NOT NULL",
          "origin_differs_from_destination": "origin <> dest",
          "arrives_after_departure": "sched_arr_min > sched_dep_min"}),
    Spec("silver", "crew_pairings", silver_crew_pairings, ("bronze.crew_pairings",),
         "Crew pairings (one row each)."),
    Spec("silver", "crew_legs", silver_crew_legs, ("bronze.crew_pairings",),
         "Legs worked by each pairing, in order."),
    Spec("silver", "crew_members", silver_crew_members, ("bronze.crew_pairings",),
         "Crew members with decoded positions.",
         {"known_position": "role IS NOT NULL"}),
    Spec("silver", "bookings", silver_bookings,
         ("bronze.reservations_cdc", "silver.flights"),
         "Confirmed reservation segments after applying inserts, updates and deletes.",
         {"flight_on_schedule": "on_schedule"}),
    Spec("silver", "passengers", silver_passengers,
         ("silver.bookings", "bronze.loyalty_members", "semantic.loyalty_tiers"),
         "Customers with a confirmed segment and their current loyalty tier.",
         {"known_tier": "tier IS NOT NULL"}),
    Spec("silver", "flight_status", silver_flight_status,
         ("bronze.ops_events", "silver.flights"),
         "Latest estimated departure per flight from the ops event stream.",
         {"valid_estimate": "est_dep_min IS NOT NULL"}),
]

# Graph tables: comment and schema come from the ontology (see utilities/ontology.py).
GOLD_GRAPH = {
    "node_airport": (gold_node_airport, ("silver.airports",)),
    "node_aircraft": (gold_node_aircraft, ("silver.aircraft",)),
    "node_flight": (gold_node_flight, ("silver.flights", "silver.aircraft", "silver.bookings",
                                       "silver.flight_status")),
    "node_crew": (gold_node_crew, ("silver.crew_members", "silver.crew_legs", "silver.flights",
                                   "semantic.business_rules")),
    "node_passenger": (gold_node_passenger, ("silver.passengers",)),
    "node_business_rule": (gold_node_business_rule, ("semantic.business_rules",)),
    "edge_route": (gold_edge_route, ("silver.routes",)),
    "edge_departs_from": (gold_edge_departs_from, ("silver.flights",)),
    "edge_arrives_at": (gold_edge_arrives_at, ("silver.flights",)),
    "edge_operates": (gold_edge_operates, ("silver.flights",)),
    "edge_next_leg": (gold_edge_next_leg, ("silver.flights",)),
    "edge_assigned_to": (gold_edge_assigned_to, ("silver.crew_members", "silver.crew_legs")),
    "edge_booked_on": (gold_edge_booked_on, ("silver.bookings",)),
}

GOLD_ANALYTICS = [
    Spec("gold", "connections", gold_connections,
         ("silver.bookings", "silver.flights", "silver.passengers", "semantic.business_rules"),
         "Connecting itineraries with connect time and slack against MIN_CONNECT_MIN."),
    Spec("gold", "crew_duty", gold_crew_duty,
         ("silver.crew_members", "silver.crew_legs", "silver.flights",
          "semantic.business_rules"),
         "Crew pairings with duty start, scheduled release, duty limit and remaining buffer."),
]


def conform(df: DataFrame, element) -> DataFrame:
    """Select and cast a gold graph table to the ontology's column order and types."""
    props = {p.column: p.spark_type for p in element.properties}
    return df.select(*[F.col(c).cast("string").alias(c) for c in element.key_columns],
                     *[F.col(c).cast(t).alias(c) for c, t in props.items()])


def gold_graph_specs(onto) -> list[Spec]:
    specs = []
    for el in onto.elements:
        fn, inputs = GOLD_GRAPH[el.table]
        specs.append(Spec("gold", el.table, lambda *dfs, fn=fn, el=el: conform(fn(*dfs), el),
                          inputs, el.description))
    return specs


def all_specs(onto) -> list[Spec]:
    return SILVER + gold_graph_specs(onto) + GOLD_ANALYTICS


def expectation_filter(expectations: dict[str, str]) -> str:
    """The rows `expect_all_or_drop` keeps (a NULL result counts as a failure)."""
    return " AND ".join(f"COALESCE(({c}), FALSE)" for c in expectations.values()) or "TRUE"
