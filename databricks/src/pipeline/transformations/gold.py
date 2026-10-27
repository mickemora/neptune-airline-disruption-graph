# Gold: the graph projection and the analytics tables.
#
# node_* and edge_* tables are shaped by the ontology (src/ontology/airline_ops.yml):
# their columns, types and comments come from it, and the declared schema makes the
# update fail if a transform drifts from the contract the graph depends on.
# connections and crew_duty feed the metric views in airline_semantic.

from pyspark import pipelines as dp

from utilities import ontology
from utilities import transforms as T
from utilities.names import Names

N = Names(spark.conf.get("airline.catalog"))  # noqa: F821
ONTOLOGY = ontology.load()


def define(spec: T.Spec, schema: str | None = None, properties: dict | None = None) -> None:
    def build():
        return spec.build(*[spark.read.table(N.ref(i)) for i in spec.inputs])  # noqa: F821

    dp.materialized_view(name=N.table(spec.layer, spec.name), comment=spec.comment,
                         schema=schema, table_properties=properties or {})(build)


for spec in T.gold_graph_specs(ONTOLOGY):
    element = ONTOLOGY.element(spec.name)
    define(spec, element.schema_ddl(), {
        "ontology.name": ONTOLOGY.name, "ontology.version": ONTOLOGY.version,
        "graph.kind": element.kind, "graph.name": element.name,
    })

for spec in T.GOLD_ANALYTICS:
    define(spec)
