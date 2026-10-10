"""Read what the agent did in a turn from its message history (shared by runtime and UI).

Strands keeps the conversation as Bedrock-style messages; tool calls are content blocks
{"toolUse": {"name": ..., "input": {...}}}. Gateway tools carry a "<target>___" prefix,
local tools don't; both are normalised here.
"""

from __future__ import annotations


def _tool_uses(messages: list, start: int):
    for message in messages[start:]:
        for block in message.get("content", []):
            if "toolUse" in block:
                use = block["toolUse"]
                yield use["name"].split("___")[-1], use.get("input") or {}


def tools_used(messages: list, start: int) -> list[str]:
    """Names of tools called since `start`, in order."""
    return [name for name, _ in _tool_uses(messages, start)]


def analyses(messages: list, start: int) -> list[dict]:
    """Every disruption the agent simulated since `start`: [{"flightId", "delayMin"}, ...]."""
    out = []
    for name, args in _tool_uses(messages, start):
        if name == "analyze_disruption" and args.get("flight_id"):
            try:
                out.append({"flightId": str(args["flight_id"]).strip().upper(),
                            "delayMin": int(args["delay_minutes"])})
            except (KeyError, TypeError, ValueError):
                continue
    return out
