#!/usr/bin/env python3
"""Test script to discover correct Google ADK API."""

import sys

sys.path.insert(0, "/opt/anaconda3/lib/python3.12/site-packages")

import inspect

import google.adk as adk
from google.adk import Runner
from google.adk.agents import LlmAgent

print("=" * 70)
print("Google ADK API Discovery")
print("=" * 70)

# Check Runner init
print("\n1. Runner.__init__ signature:")
sig = inspect.signature(Runner.__init__)
print(sig)

# Check Runner.run
print("\n2. Runner.run signature:")
sig = inspect.signature(Runner.run)
print(sig)

# Try to create a simple agent
print("\n3. Creating a simple agent:")
try:

    def test_tool(message: str) -> str:
        """Test tool"""
        return f"Received: {message}"

    agent = LlmAgent(
        name="test", model="gemini-2.5-flash", instruction="You are a test agent", tools=[test_tool]
    )
    print(f"✓ Agent created: {agent}")
    print(f"  Agent type: {type(agent)}")
    print(f"  Agent name: {agent.name if hasattr(agent, 'name') else 'N/A'}")
except Exception as e:
    print(f"✗ Failed to create agent: {e}")

# Check for helper methods
print("\n4. Looking for run helper methods:")
for attr in dir(Runner):
    if "run" in attr.lower() and not attr.startswith("_"):
        print(f"  - Runner.{attr}")

print("\n5. Checking google.adk module:")
print(f"  Available: {[x for x in dir(adk) if not x.startswith('_')]}")
