"""Delegation round-trip tests for NexusFS → service forwarding.

Tests verify that NexusFS delegation methods correctly forward calls to
the underlying service instances with proper argument transformation.

Uses mock services (no Raft required) via object.__new__(NexusFS).

Covers:
- VersionService: 4 async methods (direct pass-through)
- ReBACService: 8 async methods (parameter renaming: zone_id→_zone_id)
- MCPService: 5 async methods (_context→context renaming)
- SkillService: 10 sync methods (result wrapping)
- LLMService: 4 methods (direct pass-through)
- OAuthService: 7 async methods (_context→context renaming)
- SearchService: 4 sync + 4 async (direct pass-through)
- ShareLinkService: 6 async methods (direct pass-through)
"""


import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.nexus_fs import NexusFS
from nexus.core.permissions import OperationContext

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_fs():
    """Create a NexusFS with mock services, bypassing __init__.

    Uses MagicMock for all services. Individual tests set AsyncMock
    on specific methods they need to await.
    """
    fs = object.__new__(NexusFS)
    fs.version_service = MagicMock()
    fs.rebac_service = MagicMock()
    fs.mcp_service = MagicMock()
    fs.skill_service = MagicMock()
    fs.llm_service = MagicMock()
    fs.oauth_service = MagicMock()
    fs.mount_service = MagicMock()
    fs.search_service = MagicMock()
    fs.share_link_service = MagicMock()
    return fs


@pytest.fixture
def context():
    """Standard operation context."""
    return OperationContext(
        user="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# VersionService Delegation (4 async methods)
# =============================================================================


class TestVersionServiceDelegation:
    """Tests for NexusFS → VersionService delegation."""

    def test_aget_version_delegates(self, mock_fs, context):
        """aget_version forwards path, version, context."""
        mock_fs.version_service.get_version = AsyncMock(return_value=b"v1data")
        result = asyncio.run(mock_fs.aget_version("/file.txt", 1, context))
        assert result == b"v1data"
        mock_fs.version_service.get_version.assert_called_once_with("/file.txt", 1, context)

    def test_alist_versions_delegates(self, mock_fs, context):
        """alist_versions forwards path and context."""
        versions = [{"version": 1}, {"version": 2}]
        mock_fs.version_service.list_versions = AsyncMock(return_value=versions)
        result = asyncio.run(mock_fs.alist_versions("/file.txt", context))
        assert result == versions
        mock_fs.version_service.list_versions.assert_called_once_with("/file.txt", context)

    def test_arollback_delegates(self, mock_fs, context):
        """arollback forwards path, version, context."""
        mock_fs.version_service.rollback = AsyncMock(return_value=None)
        asyncio.run(mock_fs.arollback("/file.txt", 2, context))
        mock_fs.version_service.rollback.assert_called_once_with("/file.txt", 2, context)

    def test_adiff_versions_delegates(self, mock_fs, context):
        """adiff_versions forwards path, v1, v2, mode, context."""
        diff = {"changed": True}
        mock_fs.version_service.diff_versions = AsyncMock(return_value=diff)
        result = asyncio.run(mock_fs.adiff_versions("/file.txt", 1, 2, "content", context))
        assert result == diff
        mock_fs.version_service.diff_versions.assert_called_once_with(
            "/file.txt", 1, 2, "content", context
        )

    def test_adiff_versions_default_mode(self, mock_fs):
        """adiff_versions defaults mode to 'metadata'."""
        mock_fs.version_service.diff_versions = AsyncMock(return_value={})
        asyncio.run(mock_fs.adiff_versions("/file.txt", 1, 2))
        mock_fs.version_service.diff_versions.assert_called_once_with(
            "/file.txt", 1, 2, "metadata", None
        )


# =============================================================================
# ReBACService Delegation (8 async methods with parameter renaming)
# =============================================================================


class TestReBACServiceDelegation:
    """Tests for NexusFS → ReBACService delegation with parameter transformation."""

    def test_arebac_create_delegates(self, mock_fs, context):
        """arebac_create forwards all args."""
        mock_fs.rebac_service.rebac_create = AsyncMock(return_value={"tuple_id": "t1"})
        result = asyncio.run(
            mock_fs.arebac_create(
                subject=("user", "alice"),
                relation="viewer",
                object=("file", "/doc.txt"),
                zone_id="z1",
                context=context,
            )
        )
        assert result == {"tuple_id": "t1"}
        mock_fs.rebac_service.rebac_create.assert_called_once_with(
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
            expires_at=None,
            zone_id="z1",
            context=context,
            column_config=None,
        )

    def test_arebac_check_delegates(self, mock_fs, context):
        """arebac_check forwards all args."""
        mock_fs.rebac_service.rebac_check = AsyncMock(return_value=True)
        result = asyncio.run(
            mock_fs.arebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
                zone_id="z1",
            )
        )
        assert result is True
        mock_fs.rebac_service.rebac_check.assert_called_once_with(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            context=None,
            zone_id="z1",
        )

    def test_arebac_expand_renames_zone_id(self, mock_fs):
        """arebac_expand transforms zone_id→_zone_id, limit→_limit."""
        mock_fs.rebac_service.rebac_expand = AsyncMock(return_value=[("user", "alice")])
        result = asyncio.run(
            mock_fs.arebac_expand(
                permission="read",
                object=("file", "/doc.txt"),
                zone_id="z1",
                limit=50,
            )
        )
        assert result == [("user", "alice")]
        mock_fs.rebac_service.rebac_expand.assert_called_once_with(
            permission="read",
            object=("file", "/doc.txt"),
            _zone_id="z1",
            _limit=50,
        )

    def test_arebac_explain_delegates(self, mock_fs, context):
        """arebac_explain forwards all args."""
        explanation = {"result": True, "reason": "direct"}
        mock_fs.rebac_service.rebac_explain = AsyncMock(return_value=explanation)
        result = asyncio.run(
            mock_fs.arebac_explain(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
                zone_id="z1",
                context=context,
            )
        )
        assert result == explanation

    def test_arebac_check_batch_renames_zone_id(self, mock_fs):
        """arebac_check_batch transforms zone_id→_zone_id."""
        checks = [
            (("user", "alice"), "read", ("file", "/a.txt")),
            (("user", "bob"), "write", ("file", "/b.txt")),
        ]
        mock_fs.rebac_service.rebac_check_batch = AsyncMock(return_value=[True, False])
        result = asyncio.run(mock_fs.arebac_check_batch(checks, zone_id="z1"))
        assert result == [True, False]
        mock_fs.rebac_service.rebac_check_batch.assert_called_once_with(
            checks=checks,
            _zone_id="z1",
        )

    def test_arebac_delete_delegates(self, mock_fs):
        """arebac_delete forwards tuple_id."""
        mock_fs.rebac_service.rebac_delete = AsyncMock(return_value=True)
        result = asyncio.run(mock_fs.arebac_delete("tuple-123"))
        assert result is True
        mock_fs.rebac_service.rebac_delete.assert_called_once_with(tuple_id="tuple-123")

    def test_arebac_list_tuples_renames_params(self, mock_fs):
        """arebac_list_tuples transforms zone_id, limit, offset."""
        tuples = [{"tuple_id": "t1"}]
        mock_fs.rebac_service.rebac_list_tuples = AsyncMock(return_value=tuples)
        result = asyncio.run(
            mock_fs.arebac_list_tuples(
                subject=("user", "alice"),
                relation="viewer",
                zone_id="z1",
                limit=25,
                offset=10,
            )
        )
        assert result == tuples
        mock_fs.rebac_service.rebac_list_tuples.assert_called_once_with(
            subject=("user", "alice"),
            relation="viewer",
            object=None,
            relation_in=None,
            _zone_id="z1",
            _limit=25,
            _offset=10,
        )

    def test_aget_namespace_delegates(self, mock_fs):
        """aget_namespace forwards object_type."""
        ns = {"relations": ["viewer", "editor"]}
        mock_fs.rebac_service.get_namespace = AsyncMock(return_value=ns)
        result = asyncio.run(mock_fs.aget_namespace("file"))
        assert result == ns
        mock_fs.rebac_service.get_namespace.assert_called_once_with(object_type="file")


# =============================================================================
# MCPService Delegation (5 async methods with _context→context renaming)
# =============================================================================


class TestMCPServiceDelegation:
    """Tests for NexusFS → MCPService delegation."""

    def test_mcp_list_mounts_delegates(self, mock_fs, context):
        """mcp_list_mounts renames _context→context."""
        mounts = [{"name": "github"}]
        mock_fs.mcp_service.mcp_list_mounts = AsyncMock(return_value=mounts)
        result = asyncio.run(mock_fs.mcp_list_mounts(tier="system", _context=context))
        assert result == mounts
        mock_fs.mcp_service.mcp_list_mounts.assert_called_once_with(
            tier="system",
            include_unmounted=True,
            context=context,
        )

    def test_mcp_list_tools_delegates(self, mock_fs, context):
        """mcp_list_tools renames _context→context."""
        tools = [{"name": "search"}]
        mock_fs.mcp_service.mcp_list_tools = AsyncMock(return_value=tools)
        result = asyncio.run(mock_fs.mcp_list_tools("github", _context=context))
        assert result == tools
        mock_fs.mcp_service.mcp_list_tools.assert_called_once_with(
            name="github",
            context=context,
        )

    def test_mcp_mount_delegates(self, mock_fs, context):
        """mcp_mount forwards all args, renames _context→context."""
        mock_fs.mcp_service.mcp_mount = AsyncMock(return_value={"status": "mounted"})
        result = asyncio.run(
            mock_fs.mcp_mount(
                name="test",
                transport="stdio",
                command="node server.js",
                _context=context,
            )
        )
        assert result == {"status": "mounted"}
        mock_fs.mcp_service.mcp_mount.assert_called_once_with(
            name="test",
            transport="stdio",
            command="node server.js",
            url=None,
            args=None,
            env=None,
            headers=None,
            description=None,
            tier="system",
            context=context,
        )

    def test_mcp_unmount_delegates(self, mock_fs, context):
        """mcp_unmount renames _context→context."""
        mock_fs.mcp_service.mcp_unmount = AsyncMock(return_value={"status": "unmounted"})
        result = asyncio.run(mock_fs.mcp_unmount("test", _context=context))
        assert result == {"status": "unmounted"}
        mock_fs.mcp_service.mcp_unmount.assert_called_once_with(name="test", context=context)

    def test_mcp_sync_delegates(self, mock_fs, context):
        """mcp_sync renames _context→context."""
        mock_fs.mcp_service.mcp_sync = AsyncMock(return_value={"synced": 3})
        result = asyncio.run(mock_fs.mcp_sync("test", _context=context))
        assert result == {"synced": 3}
        mock_fs.mcp_service.mcp_sync.assert_called_once_with(
            name="test",
            context=context,
        )


# =============================================================================
# SkillService Delegation (10 sync methods with result wrapping)
# =============================================================================


class TestSkillServiceDelegation:
    """Tests for NexusFS → SkillService delegation with result wrapping."""

    def test_skills_share_wraps_result(self, mock_fs, context):
        """skills_share wraps tuple_id in success dict."""
        mock_fs.skill_service.share = MagicMock(return_value="tuple-abc")
        result = mock_fs.skills_share("/skills/test.py", "user:bob", context)
        assert result == {
            "success": True,
            "tuple_id": "tuple-abc",
            "skill_path": "/skills/test.py",
            "share_with": "user:bob",
        }
        mock_fs.skill_service.share.assert_called_once_with("/skills/test.py", "user:bob", context)

    def test_skills_unshare_wraps_result(self, mock_fs, context):
        """skills_unshare wraps success boolean."""
        mock_fs.skill_service.unshare = MagicMock(return_value=True)
        result = mock_fs.skills_unshare("/skills/test.py", "user:bob", context)
        assert result == {
            "success": True,
            "skill_path": "/skills/test.py",
            "unshare_from": "user:bob",
        }

    def test_skills_discover_wraps_list(self, mock_fs, context):
        """skills_discover wraps list of skill dicts."""
        skill_mock = MagicMock()
        skill_mock.to_dict.return_value = {"name": "test_skill"}
        mock_fs.skill_service.discover = MagicMock(return_value=[skill_mock])
        result = mock_fs.skills_discover("all", context)
        assert result == {"skills": [{"name": "test_skill"}], "count": 1}
        mock_fs.skill_service.discover.assert_called_once_with(context, "all")

    def test_skills_subscribe_wraps_result(self, mock_fs, context):
        """skills_subscribe wraps newly_subscribed boolean."""
        mock_fs.skill_service.subscribe = MagicMock(return_value=True)
        result = mock_fs.skills_subscribe("/skills/test.py", context)
        assert result["success"] is True
        assert result["already_subscribed"] is False

    def test_skills_subscribe_already_subscribed(self, mock_fs, context):
        """skills_subscribe reports already_subscribed when False returned."""
        mock_fs.skill_service.subscribe = MagicMock(return_value=False)
        result = mock_fs.skills_subscribe("/skills/test.py", context)
        assert result["already_subscribed"] is True

    def test_skills_unsubscribe_wraps_result(self, mock_fs, context):
        """skills_unsubscribe wraps was_subscribed boolean."""
        mock_fs.skill_service.unsubscribe = MagicMock(return_value=True)
        result = mock_fs.skills_unsubscribe("/skills/test.py", context)
        assert result == {
            "success": True,
            "skill_path": "/skills/test.py",
            "was_subscribed": True,
        }

    def test_skills_get_prompt_context_delegates(self, mock_fs, context):
        """skills_get_prompt_context calls to_dict on result."""
        prompt_ctx = MagicMock()
        prompt_ctx.to_dict.return_value = {"skills": [], "total": 0}
        mock_fs.skill_service.get_prompt_context = MagicMock(return_value=prompt_ctx)
        result = mock_fs.skills_get_prompt_context(50, context)
        assert result == {"skills": [], "total": 0}
        mock_fs.skill_service.get_prompt_context.assert_called_once_with(context, 50)

    def test_skills_load_delegates(self, mock_fs, context):
        """skills_load calls to_dict on result."""
        content = MagicMock()
        content.to_dict.return_value = {"content": "# My Skill"}
        mock_fs.skill_service.load = MagicMock(return_value=content)
        result = mock_fs.skills_load("/skills/test.py", context)
        assert result == {"content": "# My Skill"}

    def test_skills_export_delegates(self, mock_fs, context):
        """skills_export forwards all args directly."""
        export_result = {"path": "/tmp/test.skill"}
        mock_fs.skill_service.export = MagicMock(return_value=export_result)
        result = mock_fs.skills_export(
            skill_path="/skills/test.py",
            format="generic",
            context=context,
        )
        assert result == export_result
        mock_fs.skill_service.export.assert_called_once_with(
            skill_path="/skills/test.py",
            skill_name=None,
            output_path=None,
            format="generic",
            include_dependencies=False,
            context=context,
        )

    def test_skills_import_delegates(self, mock_fs, context):
        """skills_import forwards all args directly."""
        import_result = {"imported": True}
        mock_fs.skill_service.import_skill = MagicMock(return_value=import_result)
        result = mock_fs.skills_import(
            source_path="/tmp/test.skill",
            context=context,
        )
        assert result == import_result
        mock_fs.skill_service.import_skill.assert_called_once_with(
            source_path="/tmp/test.skill",
            zip_bytes=None,
            zip_data=None,
            target_path=None,
            allow_overwrite=False,
            context=context,
            tier=None,
        )


# =============================================================================
# LLMService Delegation (4 methods)
# =============================================================================


class TestLLMServiceDelegation:
    """Tests for NexusFS → LLMService delegation."""

    def test_llm_read_delegates(self, mock_fs):
        """llm_read forwards all args."""
        mock_fs.llm_service.llm_read = AsyncMock(return_value="Answer text")
        result = asyncio.run(mock_fs.llm_read("/doc.txt", "What is this?", model="claude-sonnet-4"))
        assert result == "Answer text"
        mock_fs.llm_service.llm_read.assert_called_once_with(
            path="/doc.txt",
            prompt="What is this?",
            model="claude-sonnet-4",
            max_tokens=1000,
            api_key=None,
            use_search=True,
            search_mode="semantic",
            provider=None,
        )

    def test_llm_read_detailed_delegates(self, mock_fs):
        """llm_read_detailed forwards all args."""
        detail = {"answer": "text", "sources": []}
        mock_fs.llm_service.llm_read_detailed = AsyncMock(return_value=detail)
        result = asyncio.run(mock_fs.llm_read_detailed("/doc.txt", "What?", max_tokens=500))
        assert result == detail

    def test_create_llm_reader_delegates(self, mock_fs):
        """create_llm_reader forwards all args (sync)."""
        reader = MagicMock()
        mock_fs.llm_service.create_llm_reader = MagicMock(return_value=reader)
        result = mock_fs.create_llm_reader(model="gpt-4", max_context_tokens=5000)
        assert result is reader
        mock_fs.llm_service.create_llm_reader.assert_called_once_with(
            provider=None,
            model="gpt-4",
            api_key=None,
            system_prompt=None,
            max_context_tokens=5000,
        )


# =============================================================================
# OAuthService Delegation (7 async methods with _context→context renaming)
# =============================================================================


class TestOAuthServiceDelegation:
    """Tests for NexusFS → OAuthService delegation."""

    def test_oauth_list_providers_delegates(self, mock_fs, context):
        """oauth_list_providers renames _context→context."""
        providers = [{"name": "google"}]
        mock_fs.oauth_service.oauth_list_providers = AsyncMock(return_value=providers)
        result = asyncio.run(mock_fs.oauth_list_providers(_context=context))
        assert result == providers
        mock_fs.oauth_service.oauth_list_providers.assert_called_once_with(context=context)

    def test_oauth_get_auth_url_delegates(self, mock_fs):
        """oauth_get_auth_url forwards provider, redirect_uri, scopes."""
        url_result = {"url": "https://auth.example.com"}
        mock_fs.oauth_service.oauth_get_auth_url = AsyncMock(return_value=url_result)
        result = asyncio.run(mock_fs.oauth_get_auth_url("google", scopes=["email", "profile"]))
        assert result == url_result
        mock_fs.oauth_service.oauth_get_auth_url.assert_called_once_with(
            provider="google",
            redirect_uri="http://localhost:3000/oauth/callback",
            scopes=["email", "profile"],
        )

    def test_oauth_exchange_code_delegates(self, mock_fs, context):
        """oauth_exchange_code forwards all args."""
        tokens = {"access_token": "tok123"}
        mock_fs.oauth_service.oauth_exchange_code = AsyncMock(return_value=tokens)
        result = asyncio.run(
            mock_fs.oauth_exchange_code(
                provider="google",
                code="auth_code_123",
                context=context,
            )
        )
        assert result == tokens

    def test_oauth_list_credentials_delegates(self, mock_fs, context):
        """oauth_list_credentials forwards all args."""
        creds = [{"provider": "google"}]
        mock_fs.oauth_service.oauth_list_credentials = AsyncMock(return_value=creds)
        result = asyncio.run(mock_fs.oauth_list_credentials(provider="google", context=context))
        assert result == creds

    def test_oauth_revoke_credential_delegates(self, mock_fs, context):
        """oauth_revoke_credential forwards all args."""
        mock_fs.oauth_service.oauth_revoke_credential = AsyncMock(return_value={"revoked": True})
        result = asyncio.run(mock_fs.oauth_revoke_credential("google", "user@example.com", context))
        assert result == {"revoked": True}

    def test_mcp_connect_delegates(self, mock_fs, context):
        """mcp_connect forwards provider, redirect_url, context."""
        mock_fs.oauth_service.mcp_connect = AsyncMock(
            return_value={"url": "https://klavis.example.com"}
        )
        result = asyncio.run(
            mock_fs.mcp_connect("slack", redirect_url="http://localhost:3000", context=context)
        )
        assert result == {"url": "https://klavis.example.com"}
        mock_fs.oauth_service.mcp_connect.assert_called_once_with(
            provider="slack",
            redirect_url="http://localhost:3000",
            context=context,
        )


# =============================================================================
# SearchService Delegation (4 sync + 2 async)
# =============================================================================


class TestSearchServiceDelegation:
    """Tests for NexusFS → SearchService delegation."""

    def test_list_delegates(self, mock_fs, context):
        """list forwards all args to search_service.list."""
        files = ["/a.txt", "/b.txt"]
        mock_fs.search_service.list = MagicMock(return_value=files)
        result = mock_fs.list(
            path="/data",
            recursive=False,
            details=True,
            context=context,
        )
        assert result == files
        mock_fs.search_service.list.assert_called_once_with(
            path="/data",
            recursive=False,
            details=True,
            show_parsed=True,
            context=context,
            limit=None,
            cursor=None,
        )

    def test_glob_delegates(self, mock_fs, context):
        """glob forwards pattern, path, context."""
        matches = ["/data/test.py"]
        mock_fs.search_service.glob = MagicMock(return_value=matches)
        result = mock_fs.glob("*.py", path="/data", context=context)
        assert result == matches
        mock_fs.search_service.glob.assert_called_once_with(
            pattern="*.py", path="/data", context=context
        )

    def test_glob_batch_delegates(self, mock_fs, context):
        """glob_batch forwards patterns, path, context."""
        batch = {"*.py": ["/a.py"], "*.txt": ["/b.txt"]}
        mock_fs.search_service.glob_batch = MagicMock(return_value=batch)
        result = mock_fs.glob_batch(["*.py", "*.txt"], context=context)
        assert result == batch

    def test_grep_delegates(self, mock_fs, context):
        """grep forwards all args."""
        results = [{"path": "/a.py", "line": 1, "match": "import os"}]
        mock_fs.search_service.grep = MagicMock(return_value=results)
        result = mock_fs.grep(
            "import os",
            path="/src",
            ignore_case=True,
            context=context,
        )
        assert result == results
        mock_fs.search_service.grep.assert_called_once_with(
            pattern="import os",
            path="/src",
            file_pattern=None,
            ignore_case=True,
            max_results=100,
            search_mode="auto",
            context=context,
        )

    def test_asemantic_search_delegates(self, mock_fs):
        """asemantic_search forwards all args."""
        hits = [{"path": "/doc.txt", "score": 0.95}]
        mock_fs.search_service.semantic_search = AsyncMock(return_value=hits)
        result = asyncio.run(mock_fs.asemantic_search("find errors", path="/logs", limit=5))
        assert result == hits
        mock_fs.search_service.semantic_search.assert_called_once_with(
            query="find errors",
            path="/logs",
            limit=5,
            filters=None,
            search_mode="semantic",
        )

    def test_asemantic_search_index_delegates(self, mock_fs):
        """asemantic_search_index forwards path and recursive."""
        stats = {"indexed": 42}
        mock_fs.search_service.semantic_search_index = AsyncMock(return_value=stats)
        result = asyncio.run(mock_fs.asemantic_search_index(path="/data", recursive=False))
        assert result == stats


# =============================================================================
# ShareLinkService Delegation (6 async methods)
# =============================================================================


class TestShareLinkServiceDelegation:
    """Tests for NexusFS → ShareLinkService delegation."""

    def test_create_share_link_delegates(self, mock_fs, context):
        """create_share_link forwards all args."""
        link = MagicMock()
        mock_fs.share_link_service.create_share_link = AsyncMock(return_value=link)
        result = asyncio.run(
            mock_fs.create_share_link(
                path="/data/file.txt",
                permission_level="editor",
                expires_in_hours=24,
                password="secret",
                context=context,
            )
        )
        assert result is link
        mock_fs.share_link_service.create_share_link.assert_called_once_with(
            path="/data/file.txt",
            permission_level="editor",
            expires_in_hours=24,
            max_access_count=None,
            password="secret",
            context=context,
        )

    def test_get_share_link_delegates(self, mock_fs, context):
        """get_share_link forwards link_id and context."""
        link = MagicMock()
        mock_fs.share_link_service.get_share_link = AsyncMock(return_value=link)
        result = asyncio.run(mock_fs.get_share_link("link-abc", context))
        assert result is link
        mock_fs.share_link_service.get_share_link.assert_called_once_with(
            link_id="link-abc",
            context=context,
        )

    def test_list_share_links_delegates(self, mock_fs, context):
        """list_share_links forwards all args."""
        links = MagicMock()
        mock_fs.share_link_service.list_share_links = AsyncMock(return_value=links)
        result = asyncio.run(
            mock_fs.list_share_links(
                path="/data",
                include_revoked=True,
                context=context,
            )
        )
        assert result is links
        mock_fs.share_link_service.list_share_links.assert_called_once_with(
            path="/data",
            include_revoked=True,
            include_expired=False,
            context=context,
        )

    def test_revoke_share_link_delegates(self, mock_fs, context):
        """revoke_share_link forwards link_id and context."""
        mock_fs.share_link_service.revoke_share_link = AsyncMock(return_value=MagicMock())
        asyncio.run(mock_fs.revoke_share_link("link-abc", context))
        mock_fs.share_link_service.revoke_share_link.assert_called_once_with(
            link_id="link-abc",
            context=context,
        )

    def test_access_share_link_delegates(self, mock_fs, context):
        """access_share_link forwards all args."""
        access_result = MagicMock()
        mock_fs.share_link_service.access_share_link = AsyncMock(return_value=access_result)
        result = asyncio.run(
            mock_fs.access_share_link(
                link_id="link-abc",
                password="secret",
                ip_address="1.2.3.4",
                user_agent="Mozilla/5.0",
                context=context,
            )
        )
        assert result is access_result
        mock_fs.share_link_service.access_share_link.assert_called_once_with(
            link_id="link-abc",
            password="secret",
            ip_address="1.2.3.4",
            user_agent="Mozilla/5.0",
            context=context,
        )

    def test_get_share_link_access_logs_delegates(self, mock_fs, context):
        """get_share_link_access_logs forwards all args."""
        logs = MagicMock()
        mock_fs.share_link_service.get_share_link_access_logs = AsyncMock(return_value=logs)
        result = asyncio.run(
            mock_fs.get_share_link_access_logs("link-abc", limit=50, context=context)
        )
        assert result is logs
        mock_fs.share_link_service.get_share_link_access_logs.assert_called_once_with(
            link_id="link-abc",
            limit=50,
            context=context,
        )


# =============================================================================
# MountService Async Delegation (key methods)
# =============================================================================


class TestMountServiceDelegation:
    """Tests for NexusFS → MountService async delegation."""

    def test_aadd_mount_delegates(self, mock_fs, context):
        """aadd_mount forwards all args."""
        mock_fs.mount_service.add_mount = AsyncMock(return_value="mount-id")
        result = asyncio.run(
            mock_fs.aadd_mount(
                mount_point="/mnt/gdrive",
                backend_type="google_drive",
                backend_config={"token": "tok"},
                priority=5,
                readonly=True,
                context=context,
            )
        )
        assert result == "mount-id"
        mock_fs.mount_service.add_mount.assert_called_once_with(
            mount_point="/mnt/gdrive",
            backend_type="google_drive",
            backend_config={"token": "tok"},
            priority=5,
            readonly=True,
            context=context,
        )

    def test_aremove_mount_delegates(self, mock_fs, context):
        """aremove_mount forwards mount_point and _context."""
        mock_fs.mount_service.remove_mount = AsyncMock(return_value={"removed": True})
        result = asyncio.run(mock_fs.aremove_mount("/mnt/gdrive", context))
        assert result == {"removed": True}

    def test_alist_mounts_delegates(self, mock_fs, context):
        """alist_mounts forwards _context."""
        mounts = [{"mount_point": "/mnt/test"}]
        mock_fs.mount_service.list_mounts = AsyncMock(return_value=mounts)
        result = asyncio.run(mock_fs.alist_mounts(context))
        assert result == mounts

    def test_aget_mount_delegates(self, mock_fs):
        """aget_mount forwards mount_point."""
        mount = {"mount_point": "/mnt/test", "backend": "local"}
        mock_fs.mount_service.get_mount = AsyncMock(return_value=mount)
        result = asyncio.run(mock_fs.aget_mount("/mnt/test"))
        assert result == mount

    def test_ahas_mount_delegates(self, mock_fs):
        """ahas_mount forwards mount_point."""
        mock_fs.mount_service.has_mount = AsyncMock(return_value=True)
        result = asyncio.run(mock_fs.ahas_mount("/mnt/test"))
        assert result is True

    def test_asave_mount_delegates(self, mock_fs, context):
        """asave_mount forwards all args."""
        mock_fs.mount_service.save_mount = AsyncMock(return_value="saved-id")
        result = asyncio.run(
            mock_fs.asave_mount(
                mount_point="/mnt/test",
                backend_type="local",
                backend_config={"path": "/data"},
                zone_id="z1",
                context=context,
            )
        )
        assert result == "saved-id"
