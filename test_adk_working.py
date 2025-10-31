#!/usr/bin/env python3
"""Working Google ADK example."""

import sys

sys.path.insert(0, "/opt/anaconda3/lib/python3.12/site-packages")

import os

from google.adk import Runner
from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Set API key
os.environ["GOOGLE_API_KEY"] = os.environ.get("GOOGLE_API_KEY", "test-key")


# Create tools
def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"


# Create agent
agent = LlmAgent(
    name="greeter",
    model="gemini-2.5-flash",
    instruction="You are a friendly greeter. Use the greet tool to greet people.",
    tools=[greet],
)

# Create session service
session_service = InMemorySessionService()

# Create runner
runner = Runner(app_name="test-app", agent=agent, session_service=session_service)

print("=" * 70)
print("Testing Google ADK with Nexus")
print("=" * 70)

# Create user message
user_message = types.Content(role="user", parts=[types.Part(text="Please greet Alice")])

# Run agent
print("\nRunning agent...")
try:
    for event in runner.run(
        user_id="test-user", session_id="test-session", new_message=user_message
    ):
        print(f"Event: {event}")
except Exception as e:
    print(f"Error: {e}")
    import traceback

    traceback.print_exc()
