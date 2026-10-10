#!/usr/bin/env python3
"""Chat with the disruption operations agent in your terminal.

  python -m agent.chat                                   # interactive
  python -m agent.chat --ask "WN3553 is 3 hours late. What should I do?"

Uses your AWS credentials and region (AWS_PROFILE / AWS_REGION), the deployed graph API
Lambda, and Claude on Amazon Bedrock. Set AGENT_MODEL_ID to use a different Bedrock model or
inference profile.
"""

from __future__ import annotations

import argparse
import os
import sys

from strands import Agent
from strands.models import BedrockModel

from agent.prompt import SYSTEM_PROMPT
from agent.tools import ALL_TOOLS

EXAMPLES = [
    "WN3553 is going to be 3 hours late. What's the damage and what should I do first?",
    "How late can WN3553 get before any passenger misconnects?",
    "Which connections through DAL carry the most passengers?",
    "Find me afternoon flights from DAL to LAS with open seats.",
]


def build_agent() -> Agent:
    config = {"temperature": 0.2}
    if os.getenv("AGENT_MODEL_ID"):
        config["model_id"] = os.environ["AGENT_MODEL_ID"]
    model = BedrockModel(region_name=os.getenv("AWS_REGION") or None, **config)
    return Agent(model=model, tools=ALL_TOOLS, system_prompt=SYSTEM_PROMPT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Disruption operations agent")
    parser.add_argument("--ask", help="ask one question and exit")
    args = parser.parse_args()

    agent = build_agent()   # streams its answer and prints each tool call as it happens
    if args.ask:
        agent(args.ask)
        print()
        return

    print("Disruption operations agent. Ask about delays, crews, passengers or rebooking.")
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
