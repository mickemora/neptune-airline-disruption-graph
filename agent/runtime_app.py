"""The disruption agent as an AgentCore Runtime service (packaged as main.py).

Same agent and instructions as agent/chat.py, but:
  - tools come from AgentCore Gateway over MCP, signed with the runtime's IAM role (SigV4);
  - conversation history lives in AgentCore Memory, keyed by the runtime session ID, so a
    controller can ask follow-ups across invocations.

Request:  {"prompt": "..."}                (session comes from the runtime session ID)
Response: {"result": "...", "toolsUsed": ["analyze_disruption", ...],
           "analyses": [{"flightId": "WN3553", "delayMin": 180}, ...], "sessionId": "..."}

Environment (set by infra/agentcore_stack.py): GATEWAY_URL, MEMORY_ID, AGENT_REGION,
optional AGENT_MODEL_ID.
"""

from __future__ import annotations

import logging
import os

from bedrock_agentcore import BedrockAgentCoreApp
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

from agent.prompt import SYSTEM_PROMPT
from agent.turns import analyses, tools_used

log = logging.getLogger("airline-ops-agent")
logging.basicConfig(level=logging.INFO)

REGION = (os.environ.get("AGENT_REGION") or os.environ.get("AWS_REGION")
          or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
GATEWAY_URL = os.environ["GATEWAY_URL"]
MEMORY_ID = os.environ.get("MEMORY_ID")
ACTOR_ID = "noc-controller"

app = BedrockAgentCoreApp()


def _gateway_client() -> MCPClient:
    return MCPClient(lambda: aws_iam_streamablehttp_client(
        endpoint=GATEWAY_URL, aws_region=REGION, aws_service="bedrock-agentcore"))


def _session_manager(session_id: str):
    if not MEMORY_ID:
        return None
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager)
    config = AgentCoreMemoryConfig(memory_id=MEMORY_ID, session_id=session_id,
                                   actor_id=ACTOR_ID)
    return AgentCoreMemorySessionManager(agentcore_memory_config=config, region_name=REGION)


@app.entrypoint
def invoke(payload, context):
    prompt = (payload or {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": 'send {"prompt": "<question>"}'}
    session_id = getattr(context, "session_id", None) or payload.get("session_id") or "default"

    model_config = {"temperature": 0.2}
    if os.getenv("AGENT_MODEL_ID"):
        model_config["model_id"] = os.environ["AGENT_MODEL_ID"]

    with _gateway_client() as gateway:
        agent = Agent(
            model=BedrockModel(region_name=REGION, **model_config),
            tools=gateway.list_tools_sync(),
            system_prompt=SYSTEM_PROMPT,
            session_manager=_session_manager(session_id),
            callback_handler=None,
        )
        start = len(agent.messages)          # history restored from memory comes first
        result = agent(prompt)
        tools = tools_used(agent.messages, start)
        simulated = analyses(agent.messages, start)
    log.info("session=%s tools=%s", session_id, tools)
    return {"result": str(result), "toolsUsed": tools, "analyses": simulated,
            "sessionId": session_id}


if __name__ == "__main__":
    app.run()
