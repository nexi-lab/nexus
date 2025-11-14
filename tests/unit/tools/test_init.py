"""Unit tests for nexus.tools module."""

import pytest

# Skip langgraph tests if not installed
pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")


def test_langgraph_import():
    """Test that langgraph module can be imported."""
    from nexus.tools import langgraph

    assert langgraph is not None


def test_module_all_attribute():
    """Test that __all__ is properly defined."""
    from nexus import tools

    assert hasattr(tools, "__all__")
    assert "langgraph" in tools.__all__


def test_langgraph_accessible():
    """Test that langgraph is accessible from tools module."""
    from nexus import tools

    assert hasattr(tools, "langgraph")
    assert tools.langgraph is not None
