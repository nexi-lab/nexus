"""MCP extractor: enumerate tools from tool_profiles.yaml."""

from pathlib import Path

from scripts.surface_coverage.extract_mcp import extract_mcp_tools


def test_extract_mcp_from_fixture(tmp_path: Path):
    fixture = tmp_path / "tool_profiles.yaml"
    fixture.write_text(
        "profiles:\n"
        "  default:\n"
        "    tools:\n"
        "      - nexus_fs_read\n"
        "      - nexus_fs_write\n"
        "  agent:\n"
        "    tools:\n"
        "      - nexus_fs_read\n"
        "      - nexus_search_grep\n"
    )
    results = extract_mcp_tools(fixture)
    names = {r.name for r in results}
    assert names == {"nexus_fs_read", "nexus_fs_write", "nexus_search_grep"}
    # source should reference the fixture path
    assert all(str(fixture) in r.source for r in results)


def test_extract_mcp_real_file_smoke(repo_root: Path):
    """Smoke test against the real tool_profiles.yaml - just verify it parses."""
    real = repo_root / "src/nexus/config/tool_profiles.yaml"
    if not real.exists():
        return
    results = extract_mcp_tools(real)
    # don't assert specific tools - those change. just assert we got something.
    assert len(results) > 0
    assert all(r.name.startswith("nexus_") for r in results)
