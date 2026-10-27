"""Run the Databricks pipeline's transformations locally on Spark and check the results.

The strongest check: source files -> bronze -> silver -> gold -> export reproduces,
file for file and row for row, the graph the generator writes directly (except the
flights ops control has posted delays for, which the lakehouse marks as DELAYED).
"""

import csv
from pathlib import Path

import pytest
import yaml

from conftest import seed_rows


def _read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        return next(reader), [tuple(r) for r in reader]


@pytest.fixture(scope="module")
def onto():
    from utilities import ontology
    return ontology.load()


@pytest.fixture(scope="module")
def exported(lakehouse, onto, tmp_path_factory):
    from utilities.graph_export import export_graph
    out = tmp_path_factory.mktemp("export") / "current"
    manifest = export_graph(lambda t: lakehouse[f"gold.{t}"], onto, out, {"catalog": "test"})
    return manifest, out


def test_export_reproduces_the_generated_graph(exported, dataset, sources):
    manifest, actual = exported
    _, expected = dataset
    delays = sources[0]["expectedDelays"]
    for folder in ("nodes", "edges"):
        names = sorted(p.name for p in (expected / folder).glob("*.csv"))
        assert names == sorted(p.name for p in (actual / folder).glob("*.csv"))
        for name in names:
            e_head, e_rows = _read_csv(expected / folder / name)
            a_head, a_rows = _read_csv(actual / folder / name)
            assert a_head == e_head, name
            if name == "flights.csv":
                fid, status, est = (a_head.index(h) for h in
                                    ("flightId:String", "status:String", "estDelayMin:Int"))
                fixed = []
                for r in a_rows:
                    r = list(r)
                    if r[fid] in delays:     # posted by ops control
                        assert (r[status], int(r[est])) == ("DELAYED", delays[r[fid]])
                        r[status], r[est] = "SCHEDULED", "0"
                    else:
                        assert (r[status], r[est]) == ("SCHEDULED", "0")
                    fixed.append(tuple(r))
                a_rows = fixed
            assert sorted(a_rows) == sorted(e_rows), name
    assert manifest["counts"]["Flight"] == len(dataset[0].flights)
    assert (actual / "manifest.json").is_file()


def test_data_quality_rules_drop_exactly_the_bad_records(lakehouse, dataset, sources):
    gen, _ = dataset
    manifest, _ = sources
    count = lambda t: lakehouse[t].count()  # noqa: E731
    assert count("bronze.schedule") == len(gen.flights) + manifest["anomalies"]["corruptScheduleRows"]
    assert count("silver.flights") == len(gen.flights)
    assert count("bronze.aircraft_master") == len(gen.aircraft) + 1
    assert count("silver.aircraft") == len(gen.aircraft)
    assert count("silver.bookings") == len(gen.bookings)       # orphan booking dropped
    assert count("silver.passengers") == len(gen.passengers)
    assert count("silver.crew_members") == len(gen.crew)
    assert count("silver.flight_status") == len(manifest["expectedDelays"])


def test_connections_measure_slack_against_the_rule(lakehouse, dataset):
    from pyspark.sql import functions as F
    gen, _ = dataset
    c = lakehouse["gold.connections"]
    itineraries = {b["pnr"] for b in gen.bookings if b["segment"] == 2}
    assert c.count() == len(itineraries)
    row = c.agg(F.min("slack_min").alias("lo"), F.max("min_connect_min").alias("mct"),
                F.sum(F.col("is_priority").cast("int")).alias("prio")).first()
    assert row.mct == 30 and row.lo >= 40 - 30     # booked with >= 40 min, MCT 30
    assert row.prio > 0


def test_crew_duty_is_derived_from_the_rules(lakehouse, dataset):
    from pyspark.sql import functions as F
    gen, _ = dataset
    duty = {r.pairing_id: r for r in lakehouse["gold.crew_duty"].collect()}
    assert len(duty) == len(gen.pairings)
    for p in gen.pairings:
        assert duty[p.pairing_id].duty_limit_min == p.duty_limit
    assert lakehouse["gold.crew_duty"].agg(F.min("buffer_min")).first()[0] >= 0


def test_gold_schemas_match_the_ontology(lakehouse, onto):
    spark_types = {"String": "string", "Int": "int", "Double": "double", "Bool": "boolean"}
    for el in onto.elements:
        df = lakehouse[f"gold.{el.table}"]
        assert df.columns == el.columns, el.table
        types = dict(df.dtypes)
        for p in el.properties:
            assert types[p.column] == spark_types[p.type], (el.table, p.column)


@pytest.mark.parametrize("view", ["flight_operations", "passenger_connections", "crew_duty"])
def test_metric_view_expressions_run_on_the_gold_tables(lakehouse, view):
    """Every dimension and measure in the metric view YAML is valid SQL on its source."""
    root = Path(__file__).resolve().parent.parent / "databricks" / "src" / "semantic"
    spec = yaml.safe_load((root / "metric_views" / f"{view}.yml").read_text())
    assert str(spec["version"]) == "1.1"
    from pyspark.sql import functions as F
    df = lakehouse[spec["source"].replace("${catalog}.airline_", "")]   # e.g. gold.connections
    assert spec["comment"]
    for d in spec["dimensions"]:
        assert d["comment"], d["name"]
        df.selectExpr(f"{d['expr']} AS x").limit(1).collect()
    measures = [F.expr(f"{m['expr']} AS `{m['name']}`") for m in spec["measures"]]
    assert all(m["comment"] for m in spec["measures"])
    totals = df.agg(*measures).first()
    assert totals[0] > 0
    by_dim = df.groupBy(F.expr(spec["dimensions"][0]["expr"]).alias("d")).agg(*measures)
    assert by_dim.count() > 0


def test_changing_a_business_rule_changes_the_outputs(spark, sources):
    """Raise the minimum connect time and shorten the duty limit in 'Unity Catalog'."""
    from pyspark.sql import functions as F
    from utilities.local_run import build_all
    _, src_dir = sources
    rules, tiers = seed_rows({"MIN_CONNECT_MIN": "45", "MAX_DUTY_MIN": "600"})
    t = build_all(spark, src_dir, rules, tiers)
    c = t["gold.connections"]
    assert c.agg(F.max("min_connect_min")).first()[0] == 45
    assert c.filter("slack_min < 0 AND NOT same_aircraft").count() > 0   # now below minimum
    crew = t["gold.node_crew"].first()
    assert crew.duty_limit_min - crew.duty_start_min == 600
    rule = t["gold.node_business_rule"].filter(F.col("rule_id") == "MIN_CONNECT_MIN").first()
    assert rule.value == "45"
