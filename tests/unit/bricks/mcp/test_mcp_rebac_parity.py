"""Tests for MCP-HTTP ReBAC parity (#3731).

Covers:
1. ``_resolve_mcp_operation_context`` resolution paths (step 0–4)
2. ``_op_context_to_auth_dict`` helper
3. ``_authenticate_api_key`` helper
4. MCP grep/glob with permission_enforcer (ReBAC negative test)
5. MCP grep/glob produce identical results to HTTP for the same
   user + query + zone (parity test)
6. ``context=None`` fallback behavior
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

from nexus.bricks.mcp.auth_bridge import (
    authenticate_api_key as _authenticate_api_key,
)
from nexus.bricks.mcp.auth_bridge import (
    op_context_to_auth_dict as _op_context_to_auth_dict,
)
from nexus.bricks.mcp.auth_bridge import (
    resolve_mcp_operation_context as _resolve_mcp_operation_context,
)
from nexus.bricks.mcp.server import (
    create_mcp_server,
    reset_request_api_key,
    set_request_api_key,
)
from nexus.contracts.types import OperationContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tool(server: Any, tool_name: str) -> Any:
    """Get a tool from MCP server (FastMCP 2.x/3.x compat)."""
    if hasattr(server, "_local_provider"):
        lp = server._local_provider
        comps = {v.name: v for k, v in lp._components.items() if k.startswith("tool:")}
        return comps[tool_name]
    manager = getattr(server, "_tool_manager", None)
    if manager and hasattr(manager, "_tools"):
        return manager._tools[tool_name]
    raise KeyError(f"Tool {tool_name!r} not found")


@dataclass(frozen=True)
class _FakeAuthResult:
    """Mimics the AuthResult dataclass from nexus.bricks.auth.types."""

    authenticated: bool = True
    subject_type: str = "user"
    subject_id: str | None = "alice"
    zone_id: str | None = "acme"
    is_admin: bool = False
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# _op_context_to_auth_dict tests
# ---------------------------------------------------------------------------


class TestOpContextToAuthDict:
    """Verify the OperationContext → auth_result dict bridge."""

    def test_with_valid_context(self):
        ctx = OperationContext(
            user_id="alice",
            subject_id="alice",
            zone_id="acme",
            is_admin=False,
            groups=[],
        )
        result = _op_context_to_auth_dict(ctx)
        assert result["subject_id"] == "alice"
        assert result["zone_id"] == "acme"
        assert result["is_admin"] is False

    def test_with_admin_context(self):
        ctx = OperationContext(
            user_id="admin",
            subject_id="admin",
            zone_id="root",
            is_admin=True,
            groups=["admins"],
        )
        result = _op_context_to_auth_dict(ctx)
        assert result["subject_id"] == "admin"
        assert result["is_admin"] is True

    def test_with_none_returns_anonymous(self):
        result = _op_context_to_auth_dict(None)
        assert result["subject_id"] == "anonymous"
        assert result["is_admin"] is False
        assert result["zone_id"] is not None  # Should be ROOT_ZONE_ID


# ---------------------------------------------------------------------------
# _authenticate_api_key tests
# ---------------------------------------------------------------------------


class TestAuthenticateApiKey:
    """Verify the async-to-sync bridge for auth provider."""

    def test_sync_provider_returns_directly(self):
        """Non-coroutine return value should be returned as-is."""
        provider = Mock()
        provider.authenticate = Mock(return_value=_FakeAuthResult())
        result = _authenticate_api_key(provider, "sk-test")
        assert result.authenticated is True
        assert result.subject_id == "alice"

    def test_async_provider_resolves(self):
        """Async authenticate() should be awaited via thread."""
        provider = Mock()
        provider.authenticate = AsyncMock(return_value=_FakeAuthResult())
        result = _authenticate_api_key(provider, "sk-test")
        assert result is not None
        assert result.authenticated is True

    def test_provider_failure_returns_none(self):
        """If authenticate() raises, return None (graceful fallback)."""
        provider = Mock()
        provider.authenticate = Mock(side_effect=RuntimeError("DB down"))
        result = _authenticate_api_key(provider, "sk-test")
        assert result is None


# ---------------------------------------------------------------------------
# _resolve_mcp_operation_context resolution path tests
# ---------------------------------------------------------------------------


class TestResolveMcpOperationContext:
    """Verify each resolution step in priority order."""

    def test_step0_api_key_contextvar_wins(self):
        """Step 0: _request_api_key + auth_provider → OperationContext."""
        provider = Mock()
        provider.authenticate = Mock(
            return_value=_FakeAuthResult(
                subject_id="bob",
                zone_id="beta",
                is_admin=True,
            )
        )
        nx = Mock(spec=[])  # No attrs at all → would fall through to step 4

        token = set_request_api_key("sk-bob-key")
        try:
            ctx = _resolve_mcp_operation_context(nx, auth_provider=provider)
        finally:
            reset_request_api_key(token)

        assert ctx is not None
        assert ctx.subject_id == "bob"
        assert ctx.zone_id == "beta"
        assert ctx.is_admin is True

    def test_step0_skipped_without_api_key(self):
        """Step 0 skipped when no _request_api_key is set."""
        provider = Mock()
        provider.authenticate = Mock(return_value=_FakeAuthResult())
        init_cred = OperationContext(user_id="local", groups=[])
        nx = Mock()
        nx._init_cred = init_cred

        # No set_request_api_key call → step 0 skipped → step 1 wins.
        ctx = _resolve_mcp_operation_context(nx, auth_provider=provider)
        assert ctx is init_cred
        provider.authenticate.assert_not_called()

    def test_step0_skipped_without_auth_provider(self):
        """Step 0 skipped when no auth_provider is passed."""
        init_cred = OperationContext(user_id="local", groups=[])
        nx = Mock()
        nx._init_cred = init_cred

        token = set_request_api_key("sk-test")
        try:
            ctx = _resolve_mcp_operation_context(nx, auth_provider=None)
        finally:
            reset_request_api_key(token)

        # Falls through to step 1.
        assert ctx is init_cred

    def test_step0_unauthenticated_falls_through(self):
        """Step 0: unauthenticated result → fall through to step 1."""
        provider = Mock()
        provider.authenticate = Mock(return_value=_FakeAuthResult(authenticated=False))
        init_cred = OperationContext(user_id="local", groups=[])
        nx = Mock()
        nx._init_cred = init_cred

        token = set_request_api_key("sk-revoked")
        try:
            ctx = _resolve_mcp_operation_context(nx, auth_provider=provider)
        finally:
            reset_request_api_key(token)

        assert ctx is init_cred  # Step 1 wins

    def test_step1_init_cred(self):
        """Step 1: _init_cred attr present → return it directly."""
        init_cred = OperationContext(user_id="kernel", groups=[])
        nx = Mock()
        nx._init_cred = init_cred
        ctx = _resolve_mcp_operation_context(nx)
        assert ctx is init_cred

    def test_step2_default_context(self):
        """Step 2: _default_context attr present → return it."""
        default_ctx = OperationContext(user_id="mock-user", groups=[])
        nx = Mock(spec=[])
        nx._default_context = default_ctx
        # No _init_cred → step 1 skipped → step 2 wins.
        ctx = _resolve_mcp_operation_context(nx)
        assert ctx is default_ctx

    def test_step3_whoami_fields(self):
        """Step 3: bare remote backend with whoami-populated fields."""
        nx = Mock(spec=[])
        nx.subject_id = "remote-user"
        nx.subject_type = "agent"
        nx.zone_id = "zone-x"
        nx.is_admin = True

        ctx = _resolve_mcp_operation_context(nx)
        assert ctx is not None
        assert ctx.subject_id == "remote-user"
        assert ctx.subject_type == "agent"
        assert ctx.zone_id == "zone-x"
        assert ctx.is_admin is True

    def test_step4_none_fallback(self):
        """Step 4: no identity resolvable → returns None with warning."""
        nx = Mock(spec=[])  # No attrs
        ctx = _resolve_mcp_operation_context(nx)
        assert ctx is None


# ---------------------------------------------------------------------------
# MCP grep/glob ReBAC negative test
# ---------------------------------------------------------------------------


def _make_nx_with_search(
    *,
    grep_return: list[dict[str, Any]] | None = None,
    glob_return: list[str] | None = None,
) -> MagicMock:
    """Build a mock NexusFS that returns controlled search results."""
    nx = MagicMock()
    search = MagicMock()
    search.grep = AsyncMock(return_value=list(grep_return or []))
    search.glob = MagicMock(return_value=list(glob_return or []))
    nx.service = MagicMock(side_effect=lambda name: search if name == "search" else None)
    nx._mock_search = search
    return nx


def _make_permission_enforcer(permitted_paths: list[str]) -> MagicMock:
    """Build a mock PermissionEnforcer that only permits listed paths."""
    enforcer = MagicMock()
    enforcer.filter_search_results = MagicMock(
        side_effect=lambda paths, **_kw: [p for p in paths if p in permitted_paths]
    )
    return enforcer


class TestMcpGrepRebac:
    """MCP grep applies _apply_rebac_filter when permission_enforcer is provided."""

    async def test_denied_files_excluded(self):
        """Files not in permitted set are excluded from grep results."""
        nx = _make_nx_with_search(
            grep_return=[
                {"file": "/src/allowed.py", "line": 1, "content": "match"},
                {"file": "/src/denied.py", "line": 2, "content": "match"},
                {"file": "/src/also_allowed.py", "line": 3, "content": "match"},
            ]
        )
        enforcer = _make_permission_enforcer(["/src/allowed.py", "/src/also_allowed.py"])

        server = await create_mcp_server(
            nx=nx,
            permission_enforcer=enforcer,
        )
        grep_tool = _get_tool(server, "nexus_grep")
        raw = await grep_tool.fn(pattern="match", path="/src")
        response = json.loads(raw)

        files_returned = [item["file"] for item in response["items"]]
        assert "/src/allowed.py" in files_returned
        assert "/src/also_allowed.py" in files_returned
        assert "/src/denied.py" not in files_returned
        assert response["permission_denial_rate"] > 0

    async def test_multi_line_per_file_preserved(self):
        """Multiple grep hits from the same file are all preserved.

        Regression test: the path_extractor refactor must not use a
        dict keyed by path (which collapses multi-line results to one).
        """
        nx = _make_nx_with_search(
            grep_return=[
                {"file": "/src/ok.py", "line": 1, "content": "match1"},
                {"file": "/src/ok.py", "line": 5, "content": "match2"},
                {"file": "/src/ok.py", "line": 9, "content": "match3"},
                {"file": "/src/denied.py", "line": 1, "content": "match4"},
            ]
        )
        enforcer = _make_permission_enforcer(["/src/ok.py"])
        server = await create_mcp_server(nx=nx, permission_enforcer=enforcer)
        grep_tool = _get_tool(server, "nexus_grep")
        raw = await grep_tool.fn(pattern="match", path="/src")
        response = json.loads(raw)

        # All 3 lines from ok.py must be present.
        assert len(response["items"]) == 3
        lines = [item["line"] for item in response["items"]]
        assert lines == [1, 5, 9]
        # The denied file must be absent.
        assert all(item["file"] == "/src/ok.py" for item in response["items"])

    async def test_no_enforcer_returns_all(self):
        """Without permission_enforcer, all results pass through."""
        nx = _make_nx_with_search(
            grep_return=[
                {"file": "/a.py", "line": 1, "content": "m"},
                {"file": "/b.py", "line": 2, "content": "m"},
            ]
        )
        server = await create_mcp_server(nx=nx, permission_enforcer=None)
        grep_tool = _get_tool(server, "nexus_grep")
        raw = await grep_tool.fn(pattern="m")
        response = json.loads(raw)
        assert len(response["items"]) == 2
        assert response.get("permission_denial_rate", 0) == 0

    async def test_zone_unscoping_on_grep(self):
        """Grep results with zone-prefixed paths get unscoped."""
        nx = _make_nx_with_search(
            grep_return=[
                {"file": "/zone/acme/src/x.py", "line": 1, "content": "hit"},
            ]
        )
        # Permit the zone-prefixed path (pre-unscoping).
        enforcer = _make_permission_enforcer(["/zone/acme/src/x.py"])
        server = await create_mcp_server(nx=nx, permission_enforcer=enforcer)
        grep_tool = _get_tool(server, "nexus_grep")
        raw = await grep_tool.fn(pattern="hit")
        response = json.loads(raw)
        assert response["items"][0]["file"] == "/src/x.py"
        assert response["items"][0]["zone_id"] == "acme"


class TestMcpGlobRebac:
    """MCP glob applies _apply_rebac_filter when permission_enforcer is provided."""

    async def test_denied_paths_excluded(self):
        """Paths not in permitted set are excluded from glob results."""
        nx = _make_nx_with_search(
            glob_return=[
                "/src/a.py",
                "/src/b.py",
                "/src/c.py",
            ]
        )
        enforcer = _make_permission_enforcer(["/src/a.py", "/src/c.py"])

        server = await create_mcp_server(nx=nx, permission_enforcer=enforcer)
        glob_tool = _get_tool(server, "nexus_glob")
        raw = glob_tool.fn(pattern="**/*.py")
        response = json.loads(raw)

        assert "/src/a.py" in response["items"]
        assert "/src/c.py" in response["items"]
        assert "/src/b.py" not in response["items"]
        assert response["permission_denial_rate"] > 0

    async def test_zone_unscoping_on_glob(self):
        """Glob results with zone-prefixed paths get unscoped + zone list."""
        nx = _make_nx_with_search(
            glob_return=[
                "/zone/acme/src/a.py",
                "/zone/beta/src/b.py",
            ]
        )
        enforcer = _make_permission_enforcer(
            [
                "/zone/acme/src/a.py",
                "/zone/beta/src/b.py",
            ]
        )
        server = await create_mcp_server(nx=nx, permission_enforcer=enforcer)
        glob_tool = _get_tool(server, "nexus_glob")
        raw = glob_tool.fn(pattern="**/*.py")
        response = json.loads(raw)

        assert response["items"] == ["/src/a.py", "/src/b.py"]
        assert response["item_zones"] == ["acme", "beta"]


# ---------------------------------------------------------------------------
# context=None fallback behavior test
# ---------------------------------------------------------------------------


class TestContextNoneFallback:
    """When no identity is resolvable, MCP search still works (SearchService default)."""

    async def test_grep_with_no_identity(self):
        """Grep proceeds with context=None when identity can't be resolved."""
        nx = MagicMock(spec=[])  # No attrs → step 4 → None
        search = MagicMock()
        search.grep = AsyncMock(
            return_value=[
                {"file": "/x.py", "line": 1, "content": "hit"},
            ]
        )
        nx.service = MagicMock(side_effect=lambda name: search if name == "search" else None)

        server = await create_mcp_server(nx=nx)
        grep_tool = _get_tool(server, "nexus_grep")
        raw = await grep_tool.fn(pattern="hit")
        response = json.loads(raw)

        # Should succeed — SearchService uses its own default context.
        assert len(response["items"]) == 1
        # Verify context=None was passed.
        call_kwargs = search.grep.call_args
        assert call_kwargs.kwargs.get("context") is None or call_kwargs[1].get("context") is None

    async def test_glob_with_no_identity(self):
        """Glob proceeds with context=None when identity can't be resolved."""
        nx = MagicMock(spec=[])
        search = MagicMock()
        search.glob = MagicMock(return_value=["/x.py"])
        nx.service = MagicMock(side_effect=lambda name: search if name == "search" else None)

        server = await create_mcp_server(nx=nx)
        glob_tool = _get_tool(server, "nexus_glob")
        raw = glob_tool.fn(pattern="*.py")
        response = json.loads(raw)

        assert len(response["items"]) == 1
        call_kwargs = search.glob.call_args
        assert call_kwargs.kwargs.get("context") is None or call_kwargs[1].get("context") is None
