# Databricks lakehouse: from source systems to the Neptune graph

The AWS side of this repo answers "what breaks when this flight is late?" with a graph. This
folder answers the question before that one: **where does the graph's data come from, and
who decides what it means?**

In an airline, the schedule, the fleet, crew pairings, reservations, loyalty profiles and
ops events live in different systems with different formats. This Databricks bundle ingests
all of them, cleans and conforms them, applies **governed business definitions held in
Unity Catalog**, and publishes the result as the graph Neptune loads. It runs on
**Databricks Free Edition**.

```
 source systems (files)         Lakeflow pipeline (serverless)                    Neptune
 ----------------------    ------------------------------------------------    -------------
 scheduling   SSIM CSV  ─┐  bronze        silver              gold
 fleet master CSV        │  Auto Loader → cleaned, deduped, → node_* / edge_*  → openCypher CSV
 network plan CSV        ├─ streaming     validated MVs     (ontology schema)    export volume
 crew pairings JSON      │  tables        (expectations)    connections,          │
 reservations CDC CSV    │                     ▲             crew_duty            │ laptop relay
 loyalty JSON            │                     │                 │                ▼
 ops events JSON        ─┘   airline_semantic: business_rules,   └→ metric views  S3 → bulk load
                             loyalty_tiers, ontology_* tables       (KPIs)         → graph API
                             (Unity Catalog = the master)                           reads the
                                                                                    same rules
```

## What it shows

| Capability | Where |
|---|---|
| Multi-source ingestion with Auto Loader (CSV + nested JSON, schema contracts, rescued data) | `src/pipeline/transformations/bronze.py` |
| Change data capture: inserts, updates, deletes, re-delivered duplicates → current state | `silver_bookings` in `src/pipeline/utilities/transforms.py` |
| Data-quality expectations that drop and count bad records | `SILVER` specs in `transforms.py` (pipeline UI → Data quality) |
| Decoding source quirks: zero-padded flight numbers, HHMM + day offset, `LAST/FIRST` names, position and tier codes | `silver_*` functions |
| **Ontology as the contract** between lakehouse and graph: one YAML file drives gold schemas (typed, commented), Unity Catalog tags, queryable ontology tables and the Neptune CSV headers | `src/ontology/airline_ops.yml` |
| **Governed business rules** (minimum connect time, duty limits, hub) read by the pipeline, the metric views *and* the graph API at query time | `airline_semantic.business_rules` |
| **Semantic layer**: Unity Catalog metric views (flight operations, passenger connections, crew duty) | `src/semantic/metric_views/*.yml` |
| Orchestration and deployment as code (Databricks bundle: schemas, volumes, pipeline, job) | `databricks.yml`, `resources/` |
| Tested locally: the same transforms run on local Spark in CI, and the export must reproduce the generated graph row for row | `tests/test_lakehouse.py` |

The source files carry deliberate problems so the pipeline has real work to do: 2 corrupt
schedule rows, a retired aircraft, 316 segments moved between flights, 150 cancelled or
deleted bookings, 100 re-delivered CDC rows, a booking on a flight that isn't on the
schedule, stale loyalty records, lower-case tier codes, and a flight whose estimated
departure is revised twice (WN4608, ending at +120 minutes).

## Run it

**Prerequisites:** a [Databricks Free Edition](https://www.databricks.com/learn/free-edition)
workspace and the [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install)
(a current release; bundles need its recent support for volumes and `sync.paths`).

```bash
# 1. Log in once (creates a CLI profile; note the profile name it prints)
databricks auth login --host https://<your-workspace>.cloud.databricks.com

# 2. Deploy schemas, volumes, the pipeline and the job
cd databricks
databricks bundle validate
databricks bundle deploy

# 3. Run the whole flow: setup -> land files -> pipeline -> semantic layer -> export
databricks bundle run airline_ops_daily
```

The run takes a few minutes on serverless. In the workspace:

- **Jobs & Pipelines → airline-ops-daily**: the five tasks.
- **airline-ops-lakehouse** pipeline: the bronze → silver → gold graph, row counts, and the
  **Data quality** tab (dropped schedule rows, the unscheduled booking).
- **Catalog → workspace**: the `airline_*` schemas. Open a gold table to see the ontology's
  column comments, tags and lineage back to the source files.
- **SQL editor**: paste `src/semantic/demo_queries.sql` and run it top to bottom.

### Load the lakehouse's graph into Neptune

With the AWS stacks deployed (`cdk deploy --all` from the repo root; this also deploys the
graph API version that reads business rules from the graph):

```bash
pip install -r requirements-databricks.txt          # from the repo root
python scripts/graph.py load --from-databricks --profile <your-cli-profile>
python scripts/graph.py impact WN4608 --delay 120
```

Free Edition can't write to your S3 bucket, so `load --from-databricks` relays the files:
it downloads the export from the `graph_export` volume with the Databricks SDK, uploads it
to the stack's data bucket under `databricks/<timestamp>/`, clears the graph and starts the
bulk load. (With a full Databricks account on AWS you would point an external location at
the bucket and have the job write there directly.)

### The rule-change demo

The single best moment for a technical audience: change a definition in Unity Catalog and
watch every layer follow.

```sql
UPDATE workspace.airline_semantic.business_rules
SET value = '45', updated_at = current_timestamp()
WHERE rule_id = 'MIN_CONNECT_MIN';
```

```bash
databricks bundle run airline_ops_daily
python scripts/graph.py load --from-databricks --profile <your-cli-profile>
python scripts/graph.py impact WN4608 --delay 120     # more misconnects; "rulesApplied" shows 45
```

The `passenger_connections` metric view now reports connections below the minimum connect
time, the `BusinessRule` node in Neptune holds 45, and the impact analysis (and the AI agent)
use 45 without a code change or redeploy. Set it back to 30 the same way.

## How it fits together

- **Bronze** (`airline_bronze`): one streaming table per source, Auto Loader with declared
  schemas, plus `_source_file`, `_ingested_at` and `_rescued_data`.
- **Silver** (`airline_silver`): materialized views, one row per business key. The CDC feed
  is resolved to the current state of each segment (latest change wins; deletes and
  cancellations drop out). Expectations drop invalid times, same-airport legs, unknown crew
  positions and bookings on unscheduled flights.
- **Gold** (`airline_gold`): `node_*` and `edge_*` tables whose schema comes from the
  ontology, so a transform that drifts from the graph contract fails the update; plus
  `connections` and `crew_duty` for analytics. Crew duty limits and the hub flag are
  computed from the business rules, not copied from a source system.
- **Semantic** (`airline_semantic`): `business_rules` and `loyalty_tiers` (seeded once, then
  the master), `ontology_elements` / `ontology_properties`, and three metric views.
- **Export**: `graph_export/current/{nodes,edges}/*.csv` + `manifest.json`, in exactly the
  openCypher format the Neptune bulk loader reads.

The job's Python tasks reuse the repo's `datagen` package (synced by the bundle) to simulate
the source systems and seed the rules, so the AWS-only path and the Databricks path describe
the same day: `tests/test_lakehouse.py` checks the export against the generator's own graph.

## Develop and test locally

```bash
pip install -r requirements-dev.txt     # includes pyspark; needs Java 17+
pytest tests/test_lakehouse.py tests/test_ontology.py tests/test_bundle.py tests/test_sources.py
python -m datagen.sources --out data/sources   # look at the raw files
```

`src/pipeline/utilities/local_run.py` runs every transform and expectation on local Spark
with batch reads in place of Auto Loader.

## Free Edition notes

- Serverless only, one SQL warehouse, one active pipeline per type, limited daily compute:
  the whole flow fits comfortably.
- The bundle has no `mode: development`, so names stay `airline_*` (development mode would
  prefix them).
- Outbound network access is limited, which is why the hand-off to AWS runs from your laptop.

## Tear down

```bash
cd databricks
databricks bundle destroy        # removes the job, pipeline, schemas and volumes
```

## Not done here (on purpose)

- Real-time ops events would arrive on a stream (Kafka/Kinesis) and update Neptune directly
  with `MERGE`; the file-based event feed stands in for it.
- For very large reservation volumes, `AUTO CDC` (formerly `APPLY CHANGES`) would maintain the
  bookings table incrementally; the materialized view keeps the logic visible and testable.
- Row filters and column masks on passenger data, a Genie space over the metric views, and an
  external location for a direct S3 hand-off (that last one needs a full workspace on AWS).
