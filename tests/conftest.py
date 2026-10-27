import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "functions" / "graph_api"))
sys.path.insert(0, str(ROOT / "scripts"))

# The handler imports boto3 at module load; stub it if it isn't installed locally.
try:
    import boto3  # noqa: F401
except ImportError:
    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = lambda *a, **k: None
    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **k: None
    sys.modules.update({"boto3": boto3_stub, "botocore": botocore,
                        "botocore.config": botocore_config})


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    from datagen.generate import Generator
    out = tmp_path_factory.mktemp("graph")
    gen = Generator(seed=42).run()
    gen.write(out)
    return gen, out


# ---------------------------------------------------------------- lakehouse (Databricks)
sys.path.insert(0, str(ROOT / "databricks" / "src" / "pipeline"))


@pytest.fixture(scope="session")
def sources(dataset, tmp_path_factory):
    """The same generated day, written as the raw files each source system would send."""
    from datagen.sources import write_sources
    gen, _ = dataset
    out = tmp_path_factory.mktemp("sources")
    return write_sources(gen, out), out


@pytest.fixture(scope="session")
def spark():
    """Local Spark for the lakehouse tests (needs `pip install pyspark` and Java 17+)."""
    pyspark = pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession
    try:
        session = (SparkSession.builder.master("local[2]").appName("airline-lakehouse-tests")
                   .config("spark.sql.shuffle.partitions", "4")
                   .config("spark.ui.enabled", "false")
                   .config("spark.sql.session.timeZone", "UTC")
                   .getOrCreate())
    except Exception as exc:  # e.g. no Java runtime
        pytest.skip(f"Spark {pyspark.__version__} unavailable: {exc}")
    yield session
    session.stop()


def seed_rows(overrides: dict | None = None):
    from datagen.business_rules import BUSINESS_RULES, LOYALTY_TIERS
    rules = [(rid, (overrides or {}).get(rid, value), unit, desc, owner)
             for rid, value, unit, desc, owner in BUSINESS_RULES]
    return rules, list(LOYALTY_TIERS)


@pytest.fixture(scope="session")
def lakehouse(spark, sources):
    from utilities.local_run import build_all
    _, src_dir = sources
    rules, tiers = seed_rows()
    return build_all(spark, src_dir, rules, tiers)
