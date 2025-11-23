#!/usr/bin/env python3
"""Quick test script to verify infrastructure-level API key context works."""

from nexus.mcp import create_mcp_server, get_request_api_key, set_request_api_key


def test_context_api_key() -> None:
    """Test that context API key can be set and retrieved."""
    from nexus.mcp.server import _request_api_key

    # Initially no API key in context
    assert get_request_api_key() is None, "Context should start empty"

    # Set API key
    token = set_request_api_key("sk-test-api-key-123")
    try:
        # Verify it's set
        assert get_request_api_key() == "sk-test-api-key-123", "API key should be set in context"
        print("✓ API key successfully set in context")

        # Change it
        token2 = set_request_api_key("sk-another-key-456")
        try:
            assert get_request_api_key() == "sk-another-key-456", "API key should be updated"
            print("✓ API key successfully updated in context")
        finally:
            _request_api_key.reset(token2)

        # Should be back to first one after reset
        assert get_request_api_key() == "sk-test-api-key-123", "Should revert to previous context"
        print("✓ Context correctly reverts after reset")

    finally:
        _request_api_key.reset(token)

    # Should be None again
    assert get_request_api_key() is None, "Context should be empty after reset"
    print("✓ Context cleared after reset")


def test_server_creation() -> None:
    """Test that MCP server can be created with remote URL."""
    # Create server with remote URL (but don't connect)
    server = create_mcp_server(remote_url="http://localhost:8080", api_key="sk-default-key")

    # Check that server was created
    assert server is not None, "Server should be created"
    assert server.name == "nexus", "Default server name should be 'nexus'"

    # Check that tools are registered
    tools = list(server._tool_manager._tools.keys())
    assert "nexus_read_file" in tools, "nexus_read_file tool should be registered"
    assert "nexus_write_file" in tools, "nexus_write_file tool should be registered"
    assert "nexus_list_files" in tools, "nexus_list_files tool should be registered"

    print(f"✓ Server created with {len(tools)} tools registered")


if __name__ == "__main__":
    print("Testing infrastructure-level API key support...")
    print()

    test_context_api_key()
    print()

    test_server_creation()
    print()

    print("✅ All tests passed!")
