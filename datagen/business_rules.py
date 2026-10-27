"""Business definitions shared by the generator, the lakehouse and the graph.

In the Databricks path these rows seed the governed Unity Catalog tables
`airline_semantic.business_rules` and `airline_semantic.loyalty_tiers`. After the first
run the Unity Catalog tables are the master: edit a value there (for example raise the
minimum connect time to 45) and the next pipeline run, export and Neptune load use it.

Values are strings, as they are stored in the rules table; `int_rule` casts.
"""

from __future__ import annotations

# rule_id, value, unit, description, owner
BUSINESS_RULES = [
    ("HUB_AIRPORT", "DAL", "IATA code",
     "Airport treated as the connecting hub for the network.", "Network Planning"),
    ("MIN_CONNECT_MIN", "30", "minutes",
     "Minimum passenger connect time. A connection with less slack than this is a "
     "misconnect.", "Customer Operations"),
    ("MIN_TURN_MIN", "35", "minutes",
     "Minimum aircraft ground time between an arrival and the next departure.",
     "Network Operations Control"),
    ("CREW_MIN_CONNECT_MIN", "30", "minutes",
     "Minimum time for a crew member to change aircraft.", "Crew Planning"),
    ("CREW_REPORT_MIN", "60", "minutes",
     "Crew report time before the first departure of a pairing; the duty period starts "
     "here.", "Crew Planning"),
    ("CREW_DEBRIEF_MIN", "15", "minutes",
     "Release time after the last arrival of a pairing.", "Crew Planning"),
    ("MAX_DUTY_MIN", "780", "minutes",
     "Simplified maximum flight duty period, measured from report time.", "Crew Planning"),
]

# tier_code, tier_name, priority_rank, is_priority
LOYALTY_TIERS = [
    ("ALP", "A-List Preferred", 1, True),
    ("AL", "A-List", 2, True),
    ("RR", "Member", 3, False),
    ("NONE", "None", 4, False),
]


def rules_dict() -> dict[str, str]:
    return {rule_id: value for rule_id, value, *_ in BUSINESS_RULES}


def int_rule(rule_id: str) -> int:
    return int(rules_dict()[rule_id])


def tier_code(tier_name: str) -> str:
    return next(code for code, name, *_ in LOYALTY_TIERS if name == tier_name)
