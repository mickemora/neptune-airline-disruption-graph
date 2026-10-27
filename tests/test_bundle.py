"""Static checks on the Databricks bundle (no workspace needed): every path it points at
exists, every task calls a real command, and the names match what the code expects."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

BUNDLE = Path(__file__).resolve().parent.parent / "databricks"


@pytest.fixture(scope="module")
def resources():
    merged = {}
    for path in sorted((BUNDLE / "resources").glob("*.yml")):
        for kind, items in yaml.safe_load(path.read_text())["resources"].items():
            merged.setdefault(kind, {}).update(items)
    return merged


def test_bundle_root_config():
    cfg = yaml.safe_load((BUNDLE / "databricks.yml").read_text())
    assert cfg["include"] == ["resources/*.yml"]
    for p in cfg["sync"]["paths"]:
        assert (BUNDLE / p).resolve().is_dir(), p
    assert cfg["variables"]["catalog"]["default"] == "workspace"
    assert "mode" not in cfg["targets"]["dev"]      # would prefix the schema names


def test_schemas_and_volumes_match_the_names_module(resources):
    from utilities.names import EXPORT_VOLUME, LANDING_VOLUME, SCHEMAS
    assert {s["name"] for s in resources["schemas"].values()} == set(SCHEMAS.values())
    vols = {v["name"]: v for v in resources["volumes"].values()}
    assert vols[LANDING_VOLUME]["schema_name"].endswith("airline_landing.name}")
    assert vols[EXPORT_VOLUME]["schema_name"].endswith("airline_gold.name}")


def test_pipeline_sources_exist(resources):
    (pipe,) = resources["pipelines"].values()
    base = BUNDLE / "resources"
    root = (base / pipe["root_path"]).resolve()
    assert (root / "utilities" / "transforms.py").is_file()
    include = pipe["libraries"][0]["glob"]["include"]
    files = sorted((base / include.removesuffix("**")).resolve().glob("*.py"))
    assert [f.name for f in files] == ["bronze.py", "gold.py", "silver.py"]
    assert pipe["serverless"] is True
    assert pipe["configuration"]["airline.catalog"] == "${var.catalog}"


def test_job_tasks_run_in_order_and_call_real_commands(resources):
    (job,) = resources["jobs"].values()
    tasks = job["tasks"]
    assert [t["task_key"] for t in tasks] == ["setup_reference_data", "land_source_files",
                                             "refresh_lakehouse", "publish_semantic_layer",
                                             "export_graph"]
    for prev, t in zip(tasks, tasks[1:]):
        assert t["depends_on"] == [{"task_key": prev["task_key"]}]
    commands = []
    for t in tasks:
        if "spark_python_task" in t:
            task = t["spark_python_task"]
            assert (BUNDLE / "resources" / task["python_file"]).resolve().is_file()
            commands.append(task["parameters"][0])
            assert t["environment_key"] in {e["environment_key"] for e in job["environments"]}
    assert commands == ["setup", "land", "semantic", "export"]


def test_task_script_parses_its_arguments(tmp_path, monkeypatch):
    import importlib.util
    path = BUNDLE / "src" / "tasks" / "airline_tasks.py"
    spec = importlib.util.spec_from_file_location("airline_tasks", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The synced root may be the repo root or the databricks folder itself.
    assert mod._add_paths(str(BUNDLE.parent)) == BUNDLE / "src"
    assert mod._add_paths(str(BUNDLE)) == BUNDLE / "src"
    assert mod._sql_str("it's") == "'it\\'s'"
