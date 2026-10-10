"""Agent tools: thin Strands wrappers around agent/actions.py.

The docstrings are what the model reads to decide when and how to call each tool, so they
describe purpose, inputs and units precisely. Errors are returned as {"error": ...} so the
agent can recover (for example, look up the right flight number and try again).
"""

from __future__ import annotations

from strands import tool

from agent import actions


def _safe(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except actions.GraphError as exc:
        return {"error": str(exc)}
    except (ValueError, KeyError) as exc:
        return {"error": f"bad input: {exc}"}


@tool
def analyze_disruption(flight_id: str, delay_minutes: int) -> dict:
    """Simulate a delay on one flight and return its full knock-on impact.

    Returns the flights delayed (by the same aircraft or by crews changing aircraft), each
    affected crew pairing with projected duty end versus its legal limit, passenger groups
    who miss connections (with A-List counts), and ranked rebooking options for them.
    Call it again with different delays to compare scenarios or find thresholds.

    Args:
        flight_id: Flight number, e.g. "WN3553".
        delay_minutes: Delay to apply to that flight's departure, in minutes (e.g. 180).
    """
    return _safe(actions.analyze_disruption, flight_id, delay_minutes)


@tool
def get_flight(flight_id: str) -> dict:
    """Look up one flight: route, scheduled times, aircraft, load, open seats and crew.

    Args:
        flight_id: Flight number, e.g. "WN3553".
    """
    return _safe(actions.get_flight, flight_id)


@tool
def get_aircraft_day(tail: str) -> dict:
    """List every flight one aircraft operates today, in order (its rotation).

    Args:
        tail: Aircraft tail number, e.g. "N925DM".
    """
    return _safe(actions.get_aircraft_day, tail)


@tool
def find_flights(origin: str = "", dest: str = "", departs_after: str = "00:00",
                 departs_before: str = "23:59") -> dict:
    """Search the day's schedule. Use it to find flight numbers or alternatives.

    Returns up to 25 flights ordered by departure, with open seats.

    Args:
        origin: Departure airport code, e.g. "DAL". Empty for any.
        dest: Arrival airport code, e.g. "LAS". Empty for any.
        departs_after: Earliest departure, "HH:MM" in hub (Central) time.
        departs_before: Latest departure, "HH:MM" in hub (Central) time.
    """
    return _safe(actions.find_flights, origin or None, dest or None, departs_after,
                 departs_before)


@tool
def find_rebooking(origin: str, dest: str, not_before: str) -> dict:
    """Find direct and one-stop options with open seats from one airport to another.

    Options are ranked by arrival time. One-stop options allow at least 40 minutes to connect.

    Args:
        origin: Airport the passengers are at, e.g. "DAL".
        dest: Their final destination, e.g. "ATL".
        not_before: Earliest departure they can make, "HH:MM" in hub (Central) time.
    """
    return _safe(actions.find_rebooking, origin, dest, not_before)


@tool
def busiest_connections(hub: str = "DAL", limit: int = 10) -> dict:
    """List the inbound/outbound flight pairs carrying the most connecting passengers at a hub.

    Useful for spotting which flights are most sensitive to delay.

    Args:
        hub: Connecting airport code, default "DAL".
        limit: How many pairs to return (max 25).
    """
    return _safe(actions.busiest_connections, hub, limit)


ALL_TOOLS = [analyze_disruption, get_flight, get_aircraft_day, find_flights, find_rebooking,
             busiest_connections]
