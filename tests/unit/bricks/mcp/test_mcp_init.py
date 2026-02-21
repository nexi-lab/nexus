"""Tests for MCP __init__ module."""


def test_import_create_mcp_server():
    """Test that create_mcp_server can be imported from nexus.bricks.mcp."""
    from nexus.bricks.mcp import create_mcp_server

    assert create_mcp_server is not None
    assert callable(create_mcp_server)


def test_module_has_all():
    """Test that __all__ is defined correctly."""
    import nexus.bricks.mcp

    assert hasattr(nexus.bricks.mcp, "__all__")
    assert "create_mcp_server" in nexus.bricks.mcp.__all__


def test_all_exports_are_importable():
    """Test that all exported items can be imported."""
    import nexus.bricks.mcp

    for item in nexus.bricks.mcp.__all__:
        assert hasattr(nexus.bricks.mcp, item), f"{item} is in __all__ but not in module"
