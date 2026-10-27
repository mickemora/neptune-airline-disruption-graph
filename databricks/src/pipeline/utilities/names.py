"""Unity Catalog names for every layer, in one place.

    catalog.airline_landing.sources        volume: raw files dropped by source systems
    catalog.airline_bronze.*               raw records as ingested (Auto Loader)
    catalog.airline_silver.*               cleaned, conformed, deduplicated
    catalog.airline_gold.*                 graph projection + analytics-ready tables
    catalog.airline_gold.graph_export      volume: openCypher CSVs for Neptune
    catalog.airline_semantic.*             business rules, tiers, ontology, metric views
"""

from __future__ import annotations

SCHEMAS = {
    "landing": "airline_landing",
    "bronze": "airline_bronze",
    "silver": "airline_silver",
    "gold": "airline_gold",
    "semantic": "airline_semantic",
}
LANDING_VOLUME = "sources"
EXPORT_VOLUME = "graph_export"


class Names:
    def __init__(self, catalog: str):
        self.catalog = catalog

    def schema(self, layer: str) -> str:
        return f"{self.catalog}.{SCHEMAS[layer]}"

    def table(self, layer: str, name: str) -> str:
        return f"{self.catalog}.{SCHEMAS[layer]}.{name}"

    def ref(self, ref: str) -> str:
        """'silver.flights' -> 'workspace.airline_silver.flights'"""
        layer, name = ref.split(".", 1)
        return self.table(layer, name)

    def landing_path(self, folder: str = "") -> str:
        base = f"/Volumes/{self.catalog}/{SCHEMAS['landing']}/{LANDING_VOLUME}"
        return f"{base}/{folder}/" if folder else base

    def export_path(self) -> str:
        return f"/Volumes/{self.catalog}/{SCHEMAS['gold']}/{EXPORT_VOLUME}"
