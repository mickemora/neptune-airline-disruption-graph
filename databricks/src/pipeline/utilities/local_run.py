"""Run the whole lakehouse locally with batch reads: same transforms, same expectations.

Used by the test suite (and handy for debugging) so the pipeline logic can be checked
without a workspace. Auto Loader is replaced by a plain batch read of the same files
with the same schemas; `expect_all_or_drop` is replaced by the equivalent filter.

    tables = build_all(spark, Path("data/sources"), rules_rows, tier_rows)
    tables["gold.node_flight"].show()
"""

from __future__ import annotations

from pathlib import Path

from pyspark.sql import DataFrame, SparkSession

from utilities import ontology
from utilities import transforms as T


def read_sources(spark: SparkSession, root: Path) -> dict[str, DataFrame]:
    out = {}
    for src in T.SOURCES:
        reader = spark.read.format(src.format).schema(src.schema)
        if src.format == "csv":
            reader = reader.option("header", "true")
        out[f"bronze.{src.table}"] = T.add_ingest_metadata(reader.load(str(root / src.folder)))
    return out


def build_all(spark: SparkSession, sources: Path, rules_rows: list, tier_rows: list,
              onto=None) -> dict[str, DataFrame]:
    onto = onto or ontology.load()
    tables = read_sources(spark, sources)
    tables["semantic.business_rules"] = spark.createDataFrame(rules_rows, T.RULES_SCHEMA)
    tables["semantic.loyalty_tiers"] = spark.createDataFrame(tier_rows, T.TIERS_SCHEMA)
    for spec in T.all_specs(onto):
        df = spec.build(*[tables[i] for i in spec.inputs])
        if spec.expectations:
            df = df.filter(T.expectation_filter(spec.expectations))
        tables[f"{spec.layer}.{spec.name}"] = df.cache()
    return tables
