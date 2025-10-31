#!/usr/bin/env python3
"""Test Google ADK and Nexus installation.

Run this script to verify your environment is set up correctly before
running the full examples.

Usage:
    python test_installation.py
"""

import sys


def test_google_adk():
    """Test if Google ADK is installed and accessible."""
    print("Testing Google ADK installation...")
    try:
        from google.adk.agents import Agent, LlmAgent

        print("✓ Google ADK installed successfully")
        print(f"  - Agent class: {Agent}")
        print(f"  - LlmAgent class: {LlmAgent}")
        return True
    except ImportError as e:
        print(f"✗ Google ADK import failed: {e}")
        print("\n  Install with: pip install google-adk")
        print("  See README.md for troubleshooting")
        return False


def test_nexus():
    """Test if Nexus is installed and accessible."""
    print("\nTesting Nexus installation...")
    try:
        import nexus

        print("✓ Nexus installed successfully")

        # Try connecting
        nx = nexus.connect()
        print("✓ Nexus connection successful")
        print(f"  - Type: {type(nx)}")
        return True
    except ImportError as e:
        print(f"✗ Nexus import failed: {e}")
        print("\n  Install with: pip install nexus-ai-fs")
        return False
    except Exception as e:
        print(f"✗ Nexus connection failed: {e}")
        return False


def test_environment():
    """Test environment variables."""
    import os

    print("\nTesting environment variables...")

    google_key = os.getenv("GOOGLE_API_KEY")
    if google_key:
        print(f"✓ GOOGLE_API_KEY is set ({google_key[:10]}...)")
    else:
        print("⚠ GOOGLE_API_KEY not set (required to run examples)")
        print("  Set with: export GOOGLE_API_KEY='your-key'")

    nexus_url = os.getenv("NEXUS_URL")
    if nexus_url:
        print(f"✓ NEXUS_URL is set ({nexus_url})")
    else:
        print("ℹ NEXUS_URL not set (will use local Nexus)")

    return bool(google_key)


def main():
    """Run all installation tests."""
    print("=" * 70)
    print("Google ADK + Nexus Installation Test")
    print("=" * 70)
    print()

    # Run tests
    adk_ok = test_google_adk()
    nexus_ok = test_nexus()
    env_ok = test_environment()

    # Summary
    print("\n" + "=" * 70)
    print("Test Summary")
    print("=" * 70)

    if adk_ok and nexus_ok and env_ok:
        print("✓ All tests passed! You're ready to run the examples.")
        print("\nNext steps:")
        print("  1. python basic_adk_agent.py      # Basic agent example")
        print("  2. python multi_agent_demo.py      # Multi-agent example")
        return 0
    else:
        print("✗ Some tests failed. Please fix the issues above.")

        if not adk_ok:
            print("\n  Missing: Google ADK")
        if not nexus_ok:
            print("\n  Missing: Nexus")
        if not env_ok:
            print("\n  Missing: GOOGLE_API_KEY environment variable")

        print("\nSee README.md for installation instructions.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
