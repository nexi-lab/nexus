"""Unit tests for nexus.tools.langgraph module."""

import pytest

# Skip all tests if langchain_core is not installed
pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")


def test_get_nexus_tools_import():
    """Test that get_nexus_tools can be imported."""
    from nexus.tools.langgraph import get_nexus_tools

    assert callable(get_nexus_tools)


def test_module_all_attribute():
    """Test that __all__ is properly defined."""
    from nexus.tools import langgraph

    assert hasattr(langgraph, "__all__")
    assert "get_nexus_tools" in langgraph.__all__


def test_get_nexus_tools_accessible():
    """Test that get_nexus_tools is accessible from langgraph module."""
    from nexus.tools import langgraph

    assert hasattr(langgraph, "get_nexus_tools")
    assert callable(langgraph.get_nexus_tools)


def test_get_nexus_tools_returns_list():
    """Test that get_nexus_tools returns a list of tools."""
    from nexus.tools.langgraph import get_nexus_tools

    tools = get_nexus_tools()
    assert isinstance(tools, list)
    assert len(tools) > 0
