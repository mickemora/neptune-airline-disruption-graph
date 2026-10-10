#!/usr/bin/env python3
"""Chat with the disruption operations agent in your terminal.

  python -m agent.chat                                   # local agent, interactive
  python -m agent.chat --ask "WN3553 is 3 hours late. What should I do?"
  python -m agent.chat --remote                          # the agent hosted on AgentCore

Local mode runs the agent on your laptop: your AWS credentials, the deployed graph API
Lambda and Claude on Amazon Bedrock (AGENT_MODEL_ID picks another model).
Remote mode sends each question to the AgentCore Runtime deployed by
NeptuneAirlineAgentCoreStack; one chat is one runtime session, so follow-ups keep context
(stored in AgentCore Memory).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

AGENTCORE_STACK = "NeptuneAirlineAgentCoreStack"

EXAMPLES = [
    "WN3553 is going to be 3 hours late. What's the damage and what should I do first?",
    "How late can WN3553 get before any passenger misconnects?",
    "Which connections through DAL carry the most passengers?",
    "Find me afternoon flights from DAL to LAS with open seats.",
]


def build_agent():
    from strands import Agent
    from strands.models import BedrockModel

    from agent.prompt import SYSTEM_PROMPT
    from agent.tools import ALL_TOOLS

    config = {"temperature": 0.2}
    if os.getenv("AGENT_MODEL_ID"):
        config["model_id"] = os.environ["AGENT_MODEL_ID"]
    model = BedrockModel(region_name=os.getenv("AWS_REGION") or None, **config)
    return Agent(model=model, tools=ALL_TOOLS, system_prompt=SYSTEM_PROMPT)


class RemoteAgent:
    """Calls the agent hosted on AgentCore Runtime; one instance = one session."""

    def __init__(self):
        import boto3
        from botocore.config import Config
        outputs = boto3.client("cloudformation").describe_stacks(
            StackName=AGENTCORE_STACK)["Stacks"][0]["Outputs"]
        self.arn = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "AgentRuntimeArn")
        self.client = boto3.client("bedrock-agentcore", config=Config(
            read_timeout=600, connect_timeout=10, retries={"total_max_attempts": 1}))
        self.session_id = f"noc-chat-{uuid.uuid4().hex}"       # 41 chars (33+ required)

    def __call__(self, question: str) -> None:
        resp = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.arn, runtimeSessionId=self.session_id,
            contentType="application/json", accept="application/json",
            payload=json.dumps({"prompt": question}).encode())
        body = json.loads(resp["response"].read())
        if "error" in body:
            print(f"[agent error] {body['error']}")
            return
        if body.get("toolsUsed"):
            print("[tools: " + ", ".join(body["toolsUsed"]) + "]\n")
        print(body.get("result", body))


def main() -> None:
    parser = argparse.ArgumentParser(description="Disruption operations agent")
    parser.add_argument("--ask", help="ask one question and exit")
    parser.add_argument("--remote", action="store_true",
                        help="use the agent hosted on AgentCore instead of a local one")
    args = parser.parse_args()

    # Local: streams its answer and prints each tool call as it happens.
    # Remote: waits for the hosted agent's full answer, then prints it with the tools used.
    agent = RemoteAgent() if args.remote else build_agent()
    if args.ask:
        agent(args.ask)
        print()
        return

    where = f"hosted on AgentCore (session {agent.session_id})" if args.remote else "local"
    print(f"Disruption operations agent ({where}). Ask about delays, crews, passengers or "
          "rebooking.")
    print("Try:\n" + "\n".join(f"  - {e}" for e in EXAMPLES))
    print("Type 'exit' to quit.\n")
    while True:
        try:
            question = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in ("exit", "quit"):
            break
        if not question:
            continue
        print("\nagent> ", end="")
        try:
            agent(question)       # the agent keeps the conversation, so follow-ups work
        except Exception as exc:  # e.g. Bedrock access denied: show it and keep the session
            print(f"\n[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        print("\n")


if __name__ == "__main__":
    main()
