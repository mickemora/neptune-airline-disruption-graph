"""Write the gold graph tables as Neptune openCypher bulk-load CSVs.

Output layout matches what the Neptune loader (and the repo's own generator) uses:
    <out>/nodes/<file>.csv    :ID, :LABEL, <property>:<Type>, ...
    <out>/edges/<file>.csv    :ID, :START_ID, :END_ID, :TYPE, <property>:<Type>, ...
    <out>/manifest.json       counts per label/type, ontology version, export time

Headers, labels, relationship types and property types all come from the ontology.
"""

from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pyspark.sql import DataFrame


def _cell(value, typ: str) -> str:
    if value is None:
        return ""
    if typ == "Bool":
        return "true" if value else "false"
    if typ == "Double":
        return repr(float(value))
    return str(value)


def export_graph(read: Callable[[str], DataFrame], onto, out: Path,
                 extra_manifest: dict | None = None) -> dict:
    """`read(table)` returns the gold DataFrame for an ontology table name."""
    if out.exists():   # written in place: volumes don't support renaming folders
        shutil.rmtree(out)
    (out / "nodes").mkdir(parents=True)
    (out / "edges").mkdir(parents=True)
    counts = {}
    for el in onto.elements:
        rows = read(el.table).select(*el.columns).collect()
        rows.sort(key=lambda r: r[0])
        types = [p.type for p in el.properties]
        if el.kind == "node":
            header = [":ID", ":LABEL"]
            fixed = lambda r: [r["node_id"], el.name]  # noqa: E731
        else:
            header = [":ID", ":START_ID", ":END_ID", ":TYPE"]
            fixed = lambda r: [r["edge_id"], r["start_id"], r["end_id"], el.name]  # noqa: E731
        header += [f"{p.name}:{p.type}" for p in el.properties]
        path = out / ("nodes" if el.kind == "node" else "edges") / el.file
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            n_keys = len(el.key_columns)
            for r in rows:
                w.writerow(fixed(r) + [_cell(v, t) for v, t in zip(list(r)[n_keys:], types)])
        counts[el.name] = len(rows)
    manifest = {"source": "databricks", "ontology": onto.name,
                "ontologyVersion": onto.version, "counts": counts,
                "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **(extra_manifest or {})}
    # manifest.json is written last: its presence marks a complete export.
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
