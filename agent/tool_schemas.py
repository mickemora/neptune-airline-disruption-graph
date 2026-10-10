"""Tool definitions published through AgentCore Gateway (MCP), one per function in actions.py.

Single source of truth for the hosted agent: the CDK stack turns these into the gateway
target's inline tool schema, and gateway_lambda.py dispatches calls by the same names.
Descriptions are what the model reads, so they match the local tools' docstrings.
"""

TOOLS = [
    {
        "name": "analyze_disruption",
        "description": (
            "Simulate a delay on one flight and return its full knock-on impact: flights "
            "delayed (by the same aircraft or by crews changing aircraft), affected crew "
            "pairings with projected duty end vs legal limit, passenger groups who miss "
            "connections (with A-List counts) and ranked rebooking options. Call again with "
            "different delays to compare scenarios or find thresholds."),
        "properties": {
            "flight_id": ("string", 'Flight number, e.g. "WN3553".'),
            "delay_minutes": ("integer", "Delay to apply to the departure, in minutes."),
        },
        "required": ["flight_id", "delay_minutes"],
    },
    {
        "name": "get_flight",
        "description": "Look up one flight: route, scheduled times, aircraft, load, open "
                       "seats and crew.",
        "properties": {"flight_id": ("string", 'Flight number, e.g. "WN3553".')},
        "required": ["flight_id"],
    },
    {
        "name": "get_aircraft_day",
        "description": "List every flight one aircraft operates today, in order (its rotation).",
        "properties": {"tail": ("string", 'Aircraft tail number, e.g. "N925DM".')},
        "required": ["tail"],
    },
    {
        "name": "find_flights",
        "description": "Search the day's schedule by airports and departure window. Returns "
                       "up to 25 flights ordered by departure, with open seats.",
        "properties": {
            "origin": ("string", 'Departure airport code, e.g. "DAL". Omit for any.'),
            "dest": ("string", 'Arrival airport code, e.g. "LAS". Omit for any.'),
            "departs_after": ("string", 'Earliest departure, "HH:MM" hub (Central) time.'),
            "departs_before": ("string", 'Latest departure, "HH:MM" hub (Central) time.'),
        },
        "required": [],
    },
    {
        "name": "find_rebooking",
        "description": "Find direct and one-stop options with open seats from one airport to "
                       "another, ranked by arrival time (one-stop connections of 40+ minutes).",
        "properties": {
            "origin": ("string", 'Airport the passengers are at, e.g. "DAL".'),
            "dest": ("string", 'Their final destination, e.g. "ATL".'),
            "not_before": ("string", 'Earliest departure they can make, "HH:MM" hub time.'),
        },
        "required": ["origin", "dest", "not_before"],
    },
    {
        "name": "busiest_connections",
        "description": "List the inbound/outbound flight pairs carrying the most connecting "
                       "passengers at a hub; useful for spotting delay-sensitive flights.",
        "properties": {
            "hub": ("string", 'Connecting airport code; default "DAL".'),
            "limit": ("integer", "How many pairs to return (max 25); default 10."),
        },
        "required": [],
    },
]


def cfn_inline_payload() -> list[dict]:
    """The tools as AWS::BedrockAgentCore::GatewayTarget ToolDefinition objects."""
    return [{
        "Name": t["name"],
        "Description": t["description"],
        "InputSchema": {
            "Type": "object",
            "Properties": {name: {"Type": typ, "Description": desc}
                           for name, (typ, desc) in t["properties"].items()},
            "Required": t["required"],
        },
    } for t in TOOLS]
