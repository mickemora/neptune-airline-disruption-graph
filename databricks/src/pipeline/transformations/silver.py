# Silver: clean, decode and conform each source into one row per business key.
#
# Each table is a materialized view over bronze (and governed reference data in
# airline_semantic). Expectations drop rows that break a data-quality rule and the
# pipeline UI counts them, e.g. the two corrupt schedule rows and the booking on a
# flight that isn't on the schedule.

from pyspark import pipelines as dp

from utilities import transforms as T
from utilities.names import Names

N = Names(spark.conf.get("airline.catalog"))  # noqa: F821


def define(spec: T.Spec) -> None:
    def build():
        return spec.build(*[spark.read.table(N.ref(i)) for i in spec.inputs])  # noqa: F821

    if spec.expectations:
        build = dp.expect_all_or_drop(spec.expectations)(build)
    dp.materialized_view(name=N.table(spec.layer, spec.name), comment=spec.comment)(build)


for s in T.SILVER:
    define(s)
