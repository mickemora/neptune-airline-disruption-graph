"""Load the airline ontology (src/ontology/airline_ops.yml) and derive schemas from it.

The ontology is the contract between the lakehouse and the graph: the pipeline uses it
for the gold tables' typed schemas and comments, the semantic-layer task publishes it to
Unity Catalog, and the export task uses it to write Neptune's openCypher CSV headers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "ontology" / "airline_ops.yml"
SPARK_TYPES = {"String": "STRING", "Int": "INT", "Double": "DOUBLE", "Bool": "BOOLEAN"}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass(frozen=True)
class Property:
    name: str             # graph property (camelCase)
    type: str             # String | Int | Double | Bool
    description: str
    column: str           # gold table column (snake_case)

    @property
    def spark_type(self) -> str:
        return SPARK_TYPES[self.type]


@dataclass(frozen=True)
class Element:
    kind: str             # "node" | "edge"
    name: str             # node label or relationship type
    table: str
    file: str
    id_prefix: str
    description: str
    properties: tuple[Property, ...]
    source: str | None = None   # edges: from-label
    target: str | None = None   # edges: to-label

    @property
    def key_columns(self) -> list[str]:
        return ["node_id"] if self.kind == "node" else ["edge_id", "start_id", "end_id"]

    @property
    def columns(self) -> list[str]:
        return self.key_columns + [p.column for p in self.properties]

    def schema_ddl(self) -> str:
        """Typed, commented schema for the gold table (enforced by the pipeline)."""
        key_comments = {
            "node_id": f"Graph node id ({self.id_prefix}...).",
            "edge_id": f"Graph relationship id ({self.id_prefix}...).",
            "start_id": f"Node id of the {self.source}.",
            "end_id": f"Node id of the {self.target}.",
        }
        parts = [f"{c} STRING COMMENT '{_sql(key_comments[c])}'"
                 for c in self.key_columns]
        parts += [f"{p.column} {p.spark_type} COMMENT '{_sql(p.description)}'"
                  for p in self.properties]
        return ", ".join(parts)


@dataclass
class Ontology:
    name: str
    version: str
    owner: str
    description: str
    nodes: list[Element] = field(default_factory=list)
    edges: list[Element] = field(default_factory=list)

    @property
    def elements(self) -> list[Element]:
        return self.nodes + self.edges

    def element(self, table: str) -> Element:
        return next(e for e in self.elements if e.table == table)


def _sql(text: str) -> str:
    return " ".join(text.split()).replace("\\", "\\\\").replace("'", "\\'")


def _props(raw: list[dict]) -> tuple[Property, ...]:
    return tuple(Property(p["name"], p["type"], " ".join(p["description"].split()),
                          p.get("column") or snake(p["name"])) for p in raw)


def load(path: Path | str = DEFAULT_PATH) -> Ontology:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    onto = Ontology(raw["name"], str(raw["version"]), raw["owner"],
                    " ".join(raw["description"].split()))
    for e in raw["entities"]:
        onto.nodes.append(Element("node", e["label"], e["table"], e["file"], e["id_prefix"],
                                  " ".join(e["description"].split()), _props(e["properties"])))
    for r in raw["relationships"]:
        onto.edges.append(Element("edge", r["type"], r["table"], r["file"], r["id_prefix"],
                                  " ".join(r["description"].split()), _props(r["properties"]),
                                  r["from"], r["to"]))
    return onto
