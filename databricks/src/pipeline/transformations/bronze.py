# Bronze: ingest each source system's files from the landing volume with Auto Loader.
#
# Streaming tables pick up only new files on each update, keep every record as sent
# (strings for CSV, the source's own structure for JSON), and add the file it came from
# and when it was loaded. Rows that don't fit the declared schema land in _rescued_data
# instead of failing the load.

from pyspark import pipelines as dp

from utilities import transforms as T
from utilities.names import Names

N = Names(spark.conf.get("airline.catalog"))  # noqa: F821  (spark is provided by the pipeline)


def define_bronze(src: T.Source) -> None:
    @dp.table(name=N.table("bronze", src.table), comment=src.comment)
    def bronze_table():
        reader = (spark.readStream.format("cloudFiles")  # noqa: F821
                  .option("cloudFiles.format", src.format)
                  .option("rescuedDataColumn", "_rescued_data")
                  .schema(src.schema))
        if src.format == "csv":
            reader = reader.option("header", "true")
        return T.add_ingest_metadata(reader.load(N.landing_path(src.folder)))


for source in T.SOURCES:
    define_bronze(source)
