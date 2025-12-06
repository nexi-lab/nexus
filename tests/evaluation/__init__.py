"""MCP Server Evaluation Tests.

This module contains LLM-driven evaluation tests for the Nexus MCP server.
These tests use Claude to verify that the MCP tools are effective for real-world tasks.

IMPORTANT: These tests are excluded from automatic test runs because they:
1. Require an ANTHROPIC_API_KEY
2. Consume LLM tokens (cost)
3. Take longer to run than unit tests

To run these tests manually, see README.md in this directory.
"""
