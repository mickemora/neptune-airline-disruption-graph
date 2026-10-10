"""AgentCore Gateway target: turns MCP tool calls into calls on agent/actions.py.

The gateway invokes this Lambda with the tool's arguments as the event and the tool name in
context.client_context.custom["bedrockAgentCoreToolName"], prefixed "<target>___".
Deployed with the agent/ folder as the package root, hence the import fallback.
"""

from __future__ import annotations

import logging

try:                                   # local tests: repo root on sys.path
    from agent import actions
    from agent.tool_schemas import TOOLS
except ImportError:                    # in Lambda: agent/ is the code root
    import actions                     # type: ignore[no-redef]
    from tool_schemas import TOOLS     # type: ignore[no-redef]

log = logging.getLogger()
log.setLevel(logging.INFO)

DELIMITER = "___"
KNOWN = {t["name"]: t for t in TOOLS}


def tool_name_from(context) -> str:
    full = context.client_context.custom["bedrockAgentCoreToolName"]
    return full.split(DELIMITER, 1)[1] if DELIMITER in full else full


def handler(event, context):
    name = tool_name_from(context)
    args = {k: v for k, v in (event or {}).items() if k in KNOWN.get(name, {}).get("properties", {})}
    log.info("tool=%s args=%s", name, args)
    if name not in KNOWN:
        return {"error": f"unknown tool {name!r}"}
    try:
        return getattr(actions, name)(**args)
    except actions.GraphError as exc:
        return {"error": str(exc)}
    except (TypeError, ValueError, KeyError) as exc:
        return {"error": f"bad input: {exc}"}
