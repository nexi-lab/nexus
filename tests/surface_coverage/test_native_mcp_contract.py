"""Published MCP capabilities must have current native acceptance evidence."""

from scripts.surface_coverage.extract_mcp import extract_mcp_tools
from scripts.surface_coverage.paths import COVERAGE_YAML, REPO_ROOT
from scripts.surface_coverage.schema import load_yaml
from scripts.surface_coverage.validate import repo_references_exist


def test_mcp_inventory_matches_the_supported_catalog_and_native_journey():
    configured = {
        tool.name for tool in extract_mcp_tools(REPO_ROOT / "src/nexus/config/tool_profiles.yaml")
    }
    operations = [op for op in load_yaml(COVERAGE_YAML).operations if "mcp" in op.transports]
    assert {op.transports["mcp"].name for op in operations} == configured
    for op in operations:
        assert "scripts/test_search_agent_credentials_plugin.py" in op.correctness_test
        assert repo_references_exist(op.correctness_test, REPO_ROOT)
