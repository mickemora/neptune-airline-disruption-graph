"""The ontology is the contract between the lakehouse and the graph: check it against both."""

import csv

import pytest

yaml = pytest.importorskip("yaml")

from utilities import ontology  # noqa: E402
from utilities.names import Names  # noqa: E402


@pytest.fixture(scope="module")
def onto():
    return ontology.load()


def test_ontology_headers_match_the_graph_the_api_queries(onto, dataset):
    """Same files, labels, types and property order as the generator's Neptune CSVs."""
    _, out = dataset
    for el in onto.elements:
        folder = "nodes" if el.kind == "node" else "edges"
        with (out / folder / el.file).open() as fh:
            reader = csv.reader(fh)
            header = next(reader)
            first = next(reader)
        fixed = [":ID", ":LABEL"] if el.kind == "node" else [":ID", ":START_ID", ":END_ID", ":TYPE"]
        assert header == fixed + [f"{p.name}:{p.type}" for p in el.properties], el.file
        assert first[0].startswith(el.id_prefix), el.file
        assert (first[1] if el.kind == "node" else first[3]) == el.name
    files = {(("nodes" if e.kind == "node" else "edges"), e.file) for e in onto.elements}
    on_disk = {(p.parent.name, p.name) for p in out.glob("*/*.csv")}
    assert files == on_disk


def test_relationships_connect_declared_entities(onto):
    labels = {n.name for n in onto.nodes}
    for e in onto.edges:
        assert e.source in labels and e.target in labels, e.name
    assert len({e.table for e in onto.elements}) == len(onto.elements)


def test_every_element_has_a_gold_builder(onto):
    pytest.importorskip("pyspark")
    from utilities.transforms import GOLD_GRAPH
    assert {e.table for e in onto.elements} == set(GOLD_GRAPH)


def test_schema_ddl_is_typed_and_commented(onto):
    ddl = onto.element("node_flight").schema_ddl()
    assert ddl.startswith("node_id STRING COMMENT")
    assert "sched_dep_min INT COMMENT" in ddl and "service_date STRING" in ddl
    assert "lat DOUBLE" in onto.element("node_airport").schema_ddl()
    assert "is_hub BOOLEAN" in onto.element("node_airport").schema_ddl()
    # descriptions with quotes must not break the SQL
    assert "aircraft\\'s day" in onto.element("edge_operates").schema_ddl()


def test_names():
    n = Names("workspace")
    assert n.ref("silver.flights") == "workspace.airline_silver.flights"
    assert n.landing_path("crew") == "/Volumes/workspace/airline_landing/sources/crew/"
    assert n.export_path() == "/Volumes/workspace/airline_gold/graph_export"
