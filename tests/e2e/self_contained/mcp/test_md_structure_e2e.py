"""E2E tests for markdown structure index — Issue #3718.

Tests the full write→index→read pipeline through MCP tools against a
real NexusFS instance with CASLocalBackend.  Exercises:
    - nexus_write_file → automatic index creation
    - nexus_read_file(section=..., block_type=...)
    - nexus_read_file(section="*") → structure listing
    - nexus_read_file(section="frontmatter")
    - nexus_md_structure tool
    - Edge cases: non-md, missing section, stale index, CJK, empty doc
"""

import json

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.bricks.mcp.server import create_mcp_server
from nexus.core.config import PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# ============================================================================
# HELPERS
# ============================================================================


async def get_tool(server, tool_name: str):
    return await server.get_tool(tool_name)


# ============================================================================
# FIXTURES
# ============================================================================

SAMPLE_MD = """\
---
title: Architecture
tags: [auth, api]
---

# Overview

System architecture document.

## Authentication

Auth uses JWT tokens.

```python
def verify_token(token: str) -> bool:
    return jwt.decode(token)
```

### OAuth Flow

The OAuth flow is standard.

```yaml
oauth:
  provider: google
  scopes: [openid, email]
```

## API Design

Endpoints:

| Method | Path       |
|--------|------------|
| GET    | /api/users |
| POST   | /api/users |
| DELETE | /api/users |

## Conclusion

Final thoughts.
"""

CJK_MD = """\
# 日本語ドキュメント

概要テキスト。

## 認証セクション

```python
def 認証(トークン):
    return True
```

## APIセクション

テーブル:

| メソッド | パス |
|----------|------|
| GET      | /api |
"""


@pytest.fixture
async def nexus_fs(isolated_db, tmp_path):
    backend = CASLocalBackend(root_path=str(tmp_path / "storage"))
    nx = await create_nexus_fs(
        backend=backend,
        metadata_store=RaftMetadataStore.embedded(str(isolated_db).replace(".db", "-raft")),
        record_store=SQLAlchemyRecordStore(db_path=str(isolated_db)),
        permissions=PermissionConfig(enforce=False),
    )
    yield nx
    nx.close()


@pytest.fixture
async def mcp_server(nexus_fs):
    return await create_mcp_server(nx=nexus_fs)


@pytest.fixture
async def md_file(nexus_fs):
    """Write the sample markdown file and return its path."""
    path = "/docs/arch.md"
    nexus_fs.write(path, SAMPLE_MD.encode("utf-8"))
    return path


@pytest.fixture
async def cjk_file(nexus_fs):
    """Write the CJK markdown file."""
    path = "/docs/cjk.md"
    nexus_fs.write(path, CJK_MD.encode("utf-8"))
    return path


# ============================================================================
# CORE E2E: WRITE → INDEX → READ
# ============================================================================


class TestMdStructureE2E:
    """Core write→read round trip through MCP tools."""

    @pytest.mark.asyncio
    async def test_write_creates_index(self, mcp_server, nexus_fs, md_file):
        """Writing a .md file should automatically create a structural index."""
        # The write hook should have fired during fixture setup.
        # Verify by reading the structure.
        tool = await get_tool(mcp_server, "nexus_md_structure")
        result = await tool.fn(path=md_file)
        listing = json.loads(result)

        assert isinstance(listing, list)
        assert len(listing) >= 1

        # Should have frontmatter
        fm_entries = [e for e in listing if e.get("type") == "frontmatter"]
        assert len(fm_entries) == 1
        assert "title" in fm_entries[0]["keys"]
        assert "tags" in fm_entries[0]["keys"]

        # Should have sections
        sec_entries = [e for e in listing if e.get("type") == "section"]
        headings = [e["heading"] for e in sec_entries]
        assert "Overview" in headings
        assert "Authentication" in headings
        assert "OAuth Flow" in headings
        assert "API Design" in headings
        assert "Conclusion" in headings

    @pytest.mark.asyncio
    async def test_read_full_file_unchanged(self, mcp_server, md_file):
        """Reading without section param returns full content (backward compat)."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file)
        assert "# Overview" in result
        assert "## Authentication" in result
        assert "## API Design" in result
        assert "## Conclusion" in result

    @pytest.mark.asyncio
    async def test_read_section(self, mcp_server, md_file):
        """section='Authentication' returns only that section."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, section="Authentication")

        assert "## Authentication" in result
        assert "verify_token" in result
        assert "OAuth Flow" in result  # H3 is nested inside H2
        assert "## API Design" not in result  # sibling H2 excluded
        assert "## Conclusion" not in result

    @pytest.mark.asyncio
    async def test_read_section_case_insensitive(self, mcp_server, md_file):
        """Section lookup is case-insensitive."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, section="authentication")
        assert "## Authentication" in result
        assert "verify_token" in result

    @pytest.mark.asyncio
    async def test_read_section_substring(self, mcp_server, md_file):
        """Section lookup supports substring matching."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, section="Auth")
        assert "## Authentication" in result

    @pytest.mark.asyncio
    async def test_read_section_with_code_block_type(self, mcp_server, md_file):
        """block_type='code' filters to only code blocks within section."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, section="Authentication", block_type="code")

        assert result is not None
        assert "verify_token" in result
        # Should contain the code block content
        assert "```" in result
        # Should NOT contain section heading or prose
        assert "Auth uses JWT" not in result

    @pytest.mark.asyncio
    async def test_read_section_with_table_block_type(self, mcp_server, md_file):
        """block_type='table' filters to only tables within section."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, section="API Design", block_type="table")

        assert result is not None
        assert "/api/users" in result
        assert "GET" in result
        # Should NOT contain non-table content
        assert "Endpoints:" not in result

    @pytest.mark.asyncio
    async def test_read_section_star(self, mcp_server, md_file):
        """section='*' returns structure listing as JSON."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, section="*")

        listing = json.loads(result)
        assert isinstance(listing, list)

        sec_entries = [e for e in listing if e.get("type") == "section"]
        for entry in sec_entries:
            assert "heading" in entry
            assert "depth" in entry
            assert "tokens_est" in entry
            assert isinstance(entry["tokens_est"], int)
            assert entry["tokens_est"] > 0

    @pytest.mark.asyncio
    async def test_read_section_frontmatter(self, mcp_server, md_file):
        """section='frontmatter' returns only the YAML frontmatter."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, section="frontmatter")

        assert result is not None
        assert "title" in result
        assert "Architecture" in result
        assert "tags" in result
        # Should NOT contain any markdown content
        assert "# Overview" not in result

    @pytest.mark.asyncio
    async def test_nexus_md_structure_tool(self, mcp_server, md_file):
        """Dedicated structure tool returns listing without content."""
        tool = await get_tool(mcp_server, "nexus_md_structure")
        result = await tool.fn(path=md_file)

        listing = json.loads(result)
        sec_entries = [e for e in listing if e.get("type") == "section"]

        # Check structure has expected fields
        auth = next((e for e in sec_entries if e["heading"] == "Authentication"), None)
        assert auth is not None
        assert auth["depth"] == 2
        assert auth["tokens_est"] > 0
        assert "code" in auth["blocks"]  # has code blocks

        api = next((e for e in sec_entries if e["heading"] == "API Design"), None)
        assert api is not None
        assert "table" in api["blocks"]  # has tables


# ============================================================================
# EDGE CASES
# ============================================================================


class TestMdStructureEdgeCases:
    """Edge cases exercised end-to-end."""

    @pytest.mark.asyncio
    async def test_non_md_file_ignores_section(self, mcp_server, nexus_fs):
        """section param on non-.md files returns full content."""
        nexus_fs.write("/data.txt", b"# Not markdown\nJust text.")
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path="/data.txt", section="Not markdown")
        # Should return full content since it's not .md
        assert "# Not markdown" in result
        assert "Just text." in result

    @pytest.mark.asyncio
    async def test_missing_section_returns_error(self, mcp_server, md_file):
        """Requesting a non-existent section returns error, not full content."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, section="NonexistentSection")
        # Should NOT leak full content — should return error
        assert "Error" in result or "not found" in result
        assert "# Overview" not in result

    @pytest.mark.asyncio
    async def test_empty_md_file(self, mcp_server, nexus_fs):
        """Empty .md file should not crash."""
        nexus_fs.write("/empty.md", b"")
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path="/empty.md", section="*")
        # Should return empty listing
        listing = json.loads(result)
        sec_entries = [e for e in listing if e.get("type") == "section"]
        assert len(sec_entries) == 0

    @pytest.mark.asyncio
    async def test_md_no_headings(self, mcp_server, nexus_fs):
        """Markdown with no headings should return error for section requests."""
        nexus_fs.write("/plain.md", b"Just plain text.\nNo headings here.\n")
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path="/plain.md", section="anything")
        # Should NOT leak full content — section not found
        assert "Error" in result or "not found" in result

    @pytest.mark.asyncio
    async def test_md_frontmatter_only(self, mcp_server, nexus_fs):
        """File with only frontmatter, no content."""
        nexus_fs.write("/fm_only.md", b"---\ntitle: Test\n---\n")
        tool = await get_tool(mcp_server, "nexus_md_structure")
        result = await tool.fn(path="/fm_only.md")
        listing = json.loads(result)
        fm = [e for e in listing if e.get("type") == "frontmatter"]
        assert len(fm) == 1
        assert "title" in fm[0]["keys"]

    @pytest.mark.asyncio
    async def test_cjk_section_read(self, mcp_server, cjk_file):
        """CJK headings can be read by section with correct byte offsets."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=cjk_file, section="認証セクション")

        assert "認証セクション" in result
        assert "認証(トークン)" in result
        # Should NOT bleed into the API section
        assert "APIセクション" not in result

    @pytest.mark.asyncio
    async def test_cjk_code_block_filter(self, mcp_server, cjk_file):
        """Code block filtering works with CJK content."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=cjk_file, section="認証", block_type="code")
        assert result is not None
        assert "認証(トークン)" in result

    @pytest.mark.asyncio
    async def test_cjk_table_filter(self, mcp_server, cjk_file):
        """Table filtering works with CJK content."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=cjk_file, section="API", block_type="table")
        assert result is not None
        assert "メソッド" in result
        assert "/api" in result

    @pytest.mark.asyncio
    async def test_stale_index_self_heals(self, mcp_server, nexus_fs, md_file):
        """After overwriting a file, the index self-heals on read."""
        new_content = """\
# New Title

Completely different content.

## New Section

Brand new section.
"""
        nexus_fs.write(md_file, new_content.encode("utf-8"))

        # Read structure — should reflect the NEW content
        tool = await get_tool(mcp_server, "nexus_md_structure")
        result = await tool.fn(path=md_file)
        listing = json.loads(result)
        sec_entries = [e for e in listing if e.get("type") == "section"]
        headings = [e["heading"] for e in sec_entries]

        # Old headings should be gone
        assert "Overview" not in headings
        assert "Authentication" not in headings
        # New headings should be present
        assert "New Title" in headings
        assert "New Section" in headings

    @pytest.mark.asyncio
    async def test_write_via_mcp_tool_indexes(self, mcp_server):
        """Writing via nexus_write_file MCP tool should trigger indexing."""
        write_tool = await get_tool(mcp_server, "nexus_write_file")
        await write_tool.fn(
            path="/mcp_written.md",
            content="# Written via MCP\n\n## SubSection\n\nContent here.\n",
        )

        struct_tool = await get_tool(mcp_server, "nexus_md_structure")
        result = await struct_tool.fn(path="/mcp_written.md")
        listing = json.loads(result)
        sec_entries = [e for e in listing if e.get("type") == "section"]
        headings = [e["heading"] for e in sec_entries]
        assert "Written via MCP" in headings
        assert "SubSection" in headings

    @pytest.mark.asyncio
    async def test_heading_inside_code_fence_not_indexed(self, mcp_server, nexus_fs):
        """Headings inside code fences must not appear in the index."""
        doc = """\
## Real Heading

```markdown
# Fake Heading Inside Code
## Also Fake
```

## Another Real Heading
"""
        nexus_fs.write("/fenced.md", doc.encode("utf-8"))
        tool = await get_tool(mcp_server, "nexus_md_structure")
        result = await tool.fn(path="/fenced.md")
        listing = json.loads(result)
        sec_entries = [e for e in listing if e.get("type") == "section"]
        headings = [e["heading"] for e in sec_entries]

        assert "Real Heading" in headings
        assert "Another Real Heading" in headings
        assert "Fake Heading Inside Code" not in headings
        assert "Also Fake" not in headings

    @pytest.mark.asyncio
    async def test_structure_tool_on_nonexistent_file(self, mcp_server):
        """nexus_md_structure on non-existent file returns an error."""
        tool = await get_tool(mcp_server, "nexus_md_structure")
        result = await tool.fn(path="/does/not/exist.md")
        # Should return an error string, not crash
        assert "Error" in result or "No markdown structure" in result

    @pytest.mark.asyncio
    async def test_block_type_without_section_ignored(self, mcp_server, md_file):
        """block_type without section should return full content."""
        read_tool = await get_tool(mcp_server, "nexus_read_file")
        result = await read_tool.fn(path=md_file, block_type="code")
        # Without section, block_type is ignored — full content returned
        assert "# Overview" in result
        assert "## Authentication" in result
