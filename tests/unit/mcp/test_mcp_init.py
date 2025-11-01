"""Tests for MCP __init__ module."""


def test_import_create_mcp_server():
    """Test that create_mcp_server can be imported from nexus.mcp."""
    from nexus.mcp import create_mcp_server

    assert create_mcp_server is not None
    assert callable(create_mcp_server)


def test_module_has_all():
    """Test that __all__ is defined correctly."""
    import nexus.mcp

    assert hasattr(nexus.mcp, "__all__")
    assert "create_mcp_server" in nexus.mcp.__all__


def test_all_exports_are_importable():
    """Test that all exported items can be imported."""
    import nexus.mcp

    for item in nexus.mcp.__all__:
        assert hasattr(nexus.mcp, item), f"{item} is in __all__ but not in module"
