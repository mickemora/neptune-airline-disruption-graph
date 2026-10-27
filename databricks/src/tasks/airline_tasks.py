"""Job tasks around the Lakeflow pipeline (one script, one sub-command per task).

    setup     seed the governed reference tables: business rules and loyalty tiers
    land      simulate the source systems dropping their files in the landing volume
    semantic  publish the ontology to Unity Catalog, tag the graph tables, create the
              metric views
    export    write the gold graph tables as Neptune openCypher CSVs to a volume

Each runs as a serverless Python task:
    airline_tasks.py <command> --catalog <catalog> --files-root <bundle files path>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------- paths

def _add_paths(files_root: str) -> Path:
    """Make `utilities` (src/pipeline) and `datagen` importable; return the src folder.

    The bundle syncs the databricks/ folder and the repo's datagen/ package. Depending on
    the CLI version the synced root is the repo root or the databricks/ folder itself.
    """
    root = Path(files_root)
    candidates = [root / "databricks" / "src", root / "src"]
    if "__file__" in globals():
        candidates.insert(0, Path(__file__).resolve().parents[1])
    src = next((c for c in candidates if (c / "pipeline" / "utilities").is_dir()), None)
    if src is None:
        sys.exit(f"cannot find src/pipeline/utilities under {root}")
    sys.path.insert(0, str(src / "pipeline"))
    for base in (src.parents[1], root, root.parent):
        if (base / "datagen" / "generate.py").is_file():
            sys.path.insert(0, str(base))
            break
    return src


def _spark():
    from pyspark.sql import SparkSession
    return SparkSession.builder.getOrCreate()


def _sql_str(text: str) -> str:
    return "'" + " ".join(str(text).split()).replace("\\", "\\\\").replace("'", "\\'") + "'"


# ---------------------------------------------------------------------------- setup

def cmd_setup(args, N) -> None:
    from datagen.business_rules import BUSINESS_RULES, LOYALTY_TIERS
    spark = _spark()
    rules = N.table("semantic", "business_rules")
    tiers = N.table("semantic", "loyalty_tiers")
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {rules} (
          rule_id STRING NOT NULL COMMENT 'Rule identifier, e.g. MIN_CONNECT_MIN.',
          value STRING NOT NULL COMMENT 'Rule value as text; numeric rules are cast by consumers.',
          unit STRING COMMENT 'Unit of the value.',
          description STRING COMMENT 'What the rule means.',
          owner STRING COMMENT 'Business owner accountable for the rule.',
          updated_at TIMESTAMP COMMENT 'When the value last changed.')
        COMMENT 'Governed operating rules. The lakehouse, the semantic layer and the Neptune graph all read these values; change them here, not in code.'""")
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {tiers} (
          tier_code STRING NOT NULL COMMENT 'Code used by the loyalty platform.',
          tier_name STRING NOT NULL COMMENT 'Business name of the tier.',
          priority_rank INT COMMENT '1 = served first during irregular operations.',
          is_priority BOOLEAN COMMENT 'True for tiers protected first when rebooking.')
        COMMENT 'Loyalty tier definitions. NONE stands for customers with no membership.'""")
    # Insert missing rows only: once seeded, the table is the master and edits stick.
    rule_rows = ", ".join(f"({', '.join(_sql_str(v) for v in r)})" for r in BUSINESS_RULES)
    spark.sql(f"""
        MERGE INTO {rules} t
        USING (SELECT * FROM VALUES {rule_rows} AS s(rule_id, value, unit, description, owner)) s
        ON t.rule_id = s.rule_id
        WHEN NOT MATCHED THEN INSERT (rule_id, value, unit, description, owner, updated_at)
          VALUES (s.rule_id, s.value, s.unit, s.description, s.owner, current_timestamp())""")
    tier_rows = ", ".join(f"({_sql_str(c)}, {_sql_str(n)}, {r}, {str(p).upper()})"
                          for c, n, r, p in LOYALTY_TIERS)
    spark.sql(f"""
        MERGE INTO {tiers} t
        USING (SELECT * FROM VALUES {tier_rows}
               AS s(tier_code, tier_name, priority_rank, is_priority)) s
        ON t.tier_code = s.tier_code
        WHEN NOT MATCHED THEN INSERT *""")
    spark.table(rules).select("rule_id", "value", "unit").show(truncate=False)


# ---------------------------------------------------------------------------- land

def cmd_land(args, N) -> None:
    from datagen.generate import Generator
    from datagen.sources import write_sources
    target = Path(N.landing_path())
    if (target / "sources_manifest.json").exists() and not args.force:
        print(f"Source files already landed in {target}; pass --force to rewrite them.")
        return
    manifest = write_sources(Generator(args.seed).run(), target, args.seed)
    print(json.dumps(manifest, indent=2))


# ---------------------------------------------------------------------------- semantic

def cmd_semantic(args, N, src: Path) -> None:
    from pyspark.sql import types as T
    from utilities import ontology
    spark = _spark()
    onto = ontology.load(src / "ontology" / "airline_ops.yml")

    # 1. The ontology itself, as queryable Unity Catalog tables.
    S = T.StringType()
    elements = [(e.kind, e.name, N.table("gold", e.table), e.source, e.target, e.id_prefix,
                 e.file, e.description) for e in onto.elements]
    props = [(e.name, p.name, p.column, p.type, p.description)
             for e in onto.elements for p in e.properties]
    tables = {
        "ontology_elements": (elements, ["kind", "name", "gold_table", "from_label",
                                         "to_label", "id_prefix", "export_file", "description"],
                              f"Entities (node) and relationships (edge) of the {onto.name} "
                              f"ontology v{onto.version}: {onto.description}"),
        "ontology_properties": (props, ["element", "property", "gold_column", "graph_type",
                                        "description"],
                                "Properties of each ontology element and the gold column "
                                "that holds them."),
    }
    for name, (rows, cols, comment) in tables.items():
        schema = T.StructType([T.StructField(c, S) for c in cols])
        full = N.table("semantic", name)
        spark.createDataFrame(rows, schema).write.mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(full)
        spark.sql(f"COMMENT ON TABLE {full} IS {_sql_str(comment)}")
        print(f"published {full} ({len(rows)} rows)")

    # 2. Governed tags on the graph tables, so they can be found by ontology element.
    warnings = []
    for e in onto.elements:
        full = N.table("gold", e.table)
        tags = (f"'ontology' = '{onto.name}', 'graph_kind' = '{e.kind}', "
                f"'graph_element' = '{e.name}'")
        for stmt in (f"ALTER MATERIALIZED VIEW {full} SET TAGS ({tags})",
                     f"ALTER TABLE {full} SET TAGS ({tags})"):
            try:
                spark.sql(stmt)
                break
            except Exception as exc:  # noqa: BLE001  (record and try the next form)
                err = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        else:
            warnings.append(f"tags on {full}: {err}")

    # 3. Metric views: governed KPI definitions over the gold tables.
    for path in sorted((src / "semantic" / "metric_views").glob("*.yml")):
        body = path.read_text().replace("${catalog}", N.catalog)
        view = N.table("semantic", path.stem)
        try:
            spark.sql(f"CREATE OR REPLACE VIEW {view} WITH METRICS LANGUAGE YAML AS $$\n"
                      f"{body}$$")
            print(f"created metric view {view}")
        except Exception as exc:  # older runtimes: YAML spec 0.1 has no comments
            legacy = re.sub(r"^version: .*$", "version: 0.1", body, flags=re.M)
            legacy = re.sub(r"^\s*comment: .*\n", "", legacy, flags=re.M)
            spark.sql(f"CREATE OR REPLACE VIEW {view} WITH METRICS LANGUAGE YAML AS $$\n"
                      f"{legacy}$$")
            print(f"created metric view {view} (spec 0.1; 1.1 failed: "
                  f"{str(exc).splitlines()[0]})")
    for w in warnings:
        print("WARNING", w)


# ---------------------------------------------------------------------------- export

def cmd_export(args, N, src: Path) -> None:
    from utilities import graph_export, ontology
    spark = _spark()
    onto = ontology.load(src / "ontology" / "airline_ops.yml")
    dates = [r[0] for r in spark.table(N.table("gold", "node_flight"))
             .select("service_date").distinct().collect()]
    rules = {r["rule_id"]: r["value"] for r in
             spark.table(N.table("semantic", "business_rules")).collect()}
    out = Path(N.export_path()) / "current"
    manifest = graph_export.export_graph(
        lambda t: spark.table(N.table("gold", t)), onto, out,
        {"catalog": N.catalog, "serviceDates": sorted(dates), "businessRules": rules})
    print(json.dumps(manifest, indent=2))
    print(f"\nExported to {out}. Load it into Neptune from your laptop:\n"
          f"  python scripts/graph.py load --from-databricks --catalog {N.catalog}")


# ---------------------------------------------------------------------------- main

def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["setup", "land", "semantic", "export"])
    p.add_argument("--catalog", default="workspace")
    p.add_argument("--files-root", required=True,
                   help="the bundle's synced files folder (${workspace.file_path})")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--force", action="store_true", help="land: rewrite existing files")
    args = p.parse_args(argv)
    src = _add_paths(args.files_root)
    from utilities.names import Names
    N = Names(args.catalog)
    if args.command == "setup":
        cmd_setup(args, N)
    elif args.command == "land":
        cmd_land(args, N)
    elif args.command == "semantic":
        cmd_semantic(args, N, src)
    else:
        cmd_export(args, N, src)


if __name__ == "__main__":
    main()
