import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "functions" / "graph_api"))

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
