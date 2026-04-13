"""Tests for MCP server implementation."""

from unittest.mock import AsyncMock, Mock, patch

from nexus.bricks.mcp.server import _resolve_mcp_operation_context, create_mcp_server


class TestCreateMCPServer:
    """Test create_mcp_server function."""

    async def test_create_server_with_nx_instance(self):
        """Test creating MCP server with NexusFS instance."""
        nx = Mock()
        server = await create_mcp_server(nx=nx, name="test-server")

        assert server is not None
        assert server.name == "test-server"

    async def test_create_server_with_custom_name(self):
        """Test creating MCP server with custom name."""
        nx = Mock()
        server = await create_mcp_server(nx=nx, name="my-custom-server")

        assert server.name == "my-custom-server"

    async def test_create_server_default_name(self):
        """Test creating MCP server with default name."""
        nx = Mock()
        server = await create_mcp_server(nx=nx)

        assert server.name == "nexus"

    async def test_create_server_with_remote_url(self):
        """Test creating MCP server with remote URL."""
        with patch("nexus.connect", new_callable=AsyncMock) as mock_connect:
            mock_instance = Mock()
            mock_connect.return_value = mock_instance

            server = await create_mcp_server(remote_url="http://localhost:2026")

            mock_connect.assert_called_once_with(
                config={"profile": "remote", "url": "http://localhost:2026", "api_key": None}
            )
            assert server is not None

    async def test_create_server_auto_connect(self):
        """Test creating MCP server with auto-connect when nx is None."""
        with patch("nexus.connect", new_callable=AsyncMock) as mock_connect:
            mock_nx = Mock()
            mock_connect.return_value = mock_nx

            server = await create_mcp_server()

            mock_connect.assert_called_once()
            assert server is not None


class TestMCPServerCreation:
    """Test basic MCP server creation."""

    async def test_server_is_created(self):
        """Test that server is successfully created."""
        nx = Mock()
        server = await create_mcp_server(nx=nx)

        assert server is not None
        assert server.name == "nexus"

    async def test_server_with_mock_filesystem(self):
        """Test server creation with fully mocked filesystem."""
        nx = Mock()
        nx.read = Mock(return_value=b"test")
        nx.write = Mock()
        nx.delete = Mock()

        server = await create_mcp_server(nx=nx)

        assert server is not None


class TestMCPMain:
    """Test MCP main entry point."""

    def test_main_imports(self):
        """Test that main function can be imported."""
        from nexus.bricks.mcp.server import main

        assert main is not None
        assert callable(main)


class TestResolveMCPOperationContext:
    """Regression tests for Codex review #3 finding #1.

    The MCP grep/glob tools previously called SearchService without
    any OperationContext — ReBAC filtering silently fell back to the
    ambient default connection's identity. The fix is
    ``_resolve_mcp_operation_context`` which pulls the caller's
    identity from the NexusFS kernel ``_init_cred``, the legacy
    ``_default_context`` field, or the remote connection's
    whoami-populated fields — whichever is first available.
    Falls back to ``None`` as a last resort (SearchService's own
    default kicks in, and in remote mode the server-side auth
    layer re-validates permissions via the API key regardless).
    """

    def test_init_cred_is_preferred(self):
        """Priority (1): NexusFS kernel ``_init_cred`` wins over
        every other source. This matches how every other caller
        resolves its default context inside NexusFS."""
        from nexus.contracts.types import OperationContext

        sentinel_ctx = OperationContext(
            user_id="kernel-init",
            groups=[],
            zone_id="root",
            is_system=True,
            is_admin=True,
        )
        nx = Mock(spec=["_init_cred", "_default_context", "subject_id"])
        nx._init_cred = sentinel_ctx
        # Even if _default_context is set, init_cred takes precedence.
        nx._default_context = Mock()
        nx.subject_id = "should-be-ignored"
        ctx = _resolve_mcp_operation_context(nx)
        assert ctx is sentinel_ctx

    def test_local_default_context_is_used_when_no_init_cred(self):
        """Priority (2): legacy ``_default_context`` honoured when
        ``_init_cred`` is absent."""
        from nexus.contracts.types import OperationContext

        sentinel_ctx = OperationContext(
            user_id="local-admin",
            groups=[],
            zone_id="root",
            is_system=False,
            is_admin=True,
        )
        nx = Mock(spec=["_init_cred", "_default_context"])
        nx._init_cred = None
        nx._default_context = sentinel_ctx
        ctx = _resolve_mcp_operation_context(nx)
        assert ctx is sentinel_ctx

    def test_remote_mode_builds_context_from_whoami_fields(self):
        """Priority (3): ``subject_id`` / ``subject_type`` / ``zone_id``
        / ``is_admin`` populated by whoami are lifted into an explicit
        OperationContext when neither init_cred nor default_context
        is set (bare remote backend case)."""
        nx = Mock(spec=["_init_cred", "subject_id", "subject_type", "zone_id", "is_admin"])
        nx._init_cred = None
        nx.subject_id = "alice"
        nx.subject_type = "user"
        nx.zone_id = "acme"
        nx.is_admin = False

        ctx = _resolve_mcp_operation_context(nx)
        assert ctx.user_id == "alice"
        assert ctx.subject_id == "alice"
        assert ctx.subject_type == "user"
        assert ctx.zone_id == "acme"
        assert ctx.is_admin is False
        assert ctx.is_system is False

    def test_remote_mode_admin_flag_forwarded(self):
        """Codex review #3 finding #1: the ``is_admin`` flag from
        whoami must reach the OperationContext — otherwise a
        privileged user's ambient admin status would be silently
        dropped."""
        nx = Mock(spec=["_init_cred", "subject_id", "subject_type", "zone_id", "is_admin"])
        nx._init_cred = None
        nx.subject_id = "root-admin"
        nx.subject_type = "user"
        nx.zone_id = "root"
        nx.is_admin = True

        ctx = _resolve_mcp_operation_context(nx)
        assert ctx.is_admin is True

    def test_remote_mode_agent_subject_type_preserved(self):
        """Agent subject type is preserved so ReBAC can distinguish
        user vs agent identities."""
        nx = Mock(spec=["_init_cred", "subject_id", "subject_type", "zone_id", "is_admin"])
        nx._init_cred = None
        nx.subject_id = "agent-42"
        nx.subject_type = "agent"
        nx.zone_id = "root"
        nx.is_admin = False

        ctx = _resolve_mcp_operation_context(nx)
        assert ctx.subject_type == "agent"
        assert ctx.subject_id == "agent-42"

    def test_fallback_to_none_when_identity_unresolvable(self):
        """Priority (4): when no identity is resolvable (no init_cred,
        no default_context, no whoami fields), the helper returns
        None rather than raising.

        This unblocks local embedded deployments and the
        self-contained e2e suite which use ``create_nexus_fs`` with
        permissions disabled. SearchService's own internal
        default-context fallback takes over, and in remote mode the
        server-side auth layer re-validates permissions via the API
        key regardless.
        """
        nx = Mock(spec=["_init_cred", "_default_context", "subject_id", "zone_id", "is_admin"])
        nx._init_cred = None
        nx._default_context = None
        nx.subject_id = None
        nx.zone_id = None
        nx.is_admin = False

        ctx = _resolve_mcp_operation_context(nx)
        assert ctx is None

    def test_fallback_to_none_on_empty_string_subject(self):
        """Edge: empty-string subject_id is also not a usable
        identity — fall back to None instead of building a context
        with a bogus subject."""
        nx = Mock(
            spec=[
                "_init_cred",
                "_default_context",
                "subject_id",
                "subject_type",
                "zone_id",
                "is_admin",
            ]
        )
        nx._init_cred = None
        nx._default_context = None
        nx.subject_id = ""
        nx.subject_type = "user"
        nx.zone_id = "acme"
        nx.is_admin = False

        ctx = _resolve_mcp_operation_context(nx)
        assert ctx is None

    def test_zone_id_defaults_to_root_when_missing_in_remote_mode(self):
        """When the remote connection has no cached zone_id, the
        helper falls back to ROOT_ZONE_ID — matches the default
        behaviour of the HTTP server for missing zone headers.
        """
        from nexus.contracts.constants import ROOT_ZONE_ID

        nx = Mock(spec=["_init_cred", "subject_id", "subject_type", "zone_id", "is_admin"])
        nx._init_cred = None
        nx.subject_id = "alice"
        nx.subject_type = "user"
        nx.zone_id = None  # whoami hasn't populated yet
        nx.is_admin = False

        ctx = _resolve_mcp_operation_context(nx)
        assert ctx.zone_id == ROOT_ZONE_ID
