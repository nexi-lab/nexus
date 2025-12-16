"""Unit tests for FastAPI server auth/context behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.core.exceptions import ConflictError
from nexus.core.permissions import OperationContext
from nexus.server import fastapi_server as fas


@pytest.fixture(autouse=True)
def _restore_app_state():
    """Restore global AppState to avoid cross-test leakage."""
    saved = {
        "api_key": getattr(fas._app_state, "api_key", None),
        "auth_provider": getattr(fas._app_state, "auth_provider", None),
        "nexus_fs": getattr(fas._app_state, "nexus_fs", None),
    }
    try:
        yield
    finally:
        fas._app_state.api_key = saved["api_key"]
        fas._app_state.auth_provider = saved["auth_provider"]
        fas._app_state.nexus_fs = saved["nexus_fs"]


@pytest.mark.asyncio
async def test_get_auth_result_open_access_infers_subject_from_sk_token():
    fas._app_state.api_key = None
    fas._app_state.auth_provider = None

    # Best-effort inference format: sk-<tenant>_<user>_<...>
    token = "sk-default_admin_deadbeef_0123456789abcdef0123456789abcdef"
    auth = await fas.get_auth_result(
        authorization=f"Bearer {token}",
        x_agent_id=None,
        x_nexus_subject=None,
        x_nexus_tenant_id=None,
    )

    assert auth is not None
    assert auth["authenticated"] is True
    assert auth["subject_type"] == "user"
    assert auth["subject_id"] == "admin"
    assert auth["tenant_id"] == "default"
    assert auth["metadata"]["open_access"] is True


@pytest.mark.asyncio
async def test_get_auth_result_open_access_prefers_x_nexus_subject_over_token():
    fas._app_state.api_key = None
    fas._app_state.auth_provider = None

    token = "sk-default_admin_deadbeef_0123456789abcdef0123456789abcdef"
    auth = await fas.get_auth_result(
        authorization=f"Bearer {token}",
        x_agent_id=None,
        x_nexus_subject="user:alice",
        x_nexus_tenant_id="tenant-xyz",
    )

    assert auth is not None
    assert auth["authenticated"] is True
    assert auth["subject_type"] == "user"
    assert auth["subject_id"] == "alice"
    # x_nexus_tenant_id should flow through
    assert auth["tenant_id"] == "tenant-xyz"


def test_handle_delete_passes_context_to_filesystem():
    class FS:
        def __init__(self):
            self.calls = []

        def delete(self, path: str, context: OperationContext | None = None) -> None:
            self.calls.append((path, context))

    fs = FS()
    fas._app_state.nexus_fs = fs

    ctx = OperationContext(
        user="admin",
        groups=[],
        subject_type="user",
        subject_id="admin",
        tenant_id="default",
        is_admin=True,
    )
    params = SimpleNamespace(path="/nexus_file_structure.pdf")

    result = fas._handle_delete(params, ctx)

    assert result == {"deleted": True}
    assert fs.calls == [("/nexus_file_structure.pdf", ctx)]


def test_handle_delete_falls_back_if_filesystem_delete_has_no_context_param():
    class FS:
        def __init__(self):
            self.calls = []

        def delete(self, path: str) -> None:  # no context param
            self.calls.append(path)

    fs = FS()
    fas._app_state.nexus_fs = fs

    ctx = OperationContext(
        user="admin",
        groups=[],
        subject_type="user",
        subject_id="admin",
        tenant_id="default",
        is_admin=True,
    )
    params = SimpleNamespace(path="/file.txt")

    result = fas._handle_delete(params, ctx)

    assert result == {"deleted": True}
    assert fs.calls == ["/file.txt"]


def test_handle_rename_passes_context_to_filesystem():
    class FS:
        def __init__(self):
            self.calls = []

        def rename(
            self,
            old_path: str,
            new_path: str,
            context: OperationContext | None = None,
        ) -> None:
            self.calls.append((old_path, new_path, context))

    fs = FS()
    fas._app_state.nexus_fs = fs

    ctx = OperationContext(
        user="admin",
        groups=[],
        subject_type="user",
        subject_id="admin",
        tenant_id="default",
        is_admin=True,
    )
    params = SimpleNamespace(old_path="/a.txt", new_path="/b.txt")

    result = fas._handle_rename(params, ctx)

    assert result == {"renamed": True}
    assert fs.calls == [("/a.txt", "/b.txt", ctx)]


@pytest.mark.asyncio
async def test_auto_dispatch_injects__context_param():
    """FastAPI RPC auto-dispatch should inject context into `_context` too.

    Some RPC methods (historically skills) used `_context` rather than `context`.
    """

    async def fn(_context: OperationContext | None = None):
        assert _context is not None
        return {"subject_id": _context.subject_id, "tenant_id": _context.tenant_id}

    fas._app_state.exposed_methods = {"dummy": fn}

    ctx = OperationContext(
        user="admin",
        groups=[],
        subject_type="user",
        subject_id="admin",
        tenant_id="default",
        is_admin=True,
    )
    params = SimpleNamespace()

    result = await fas._auto_dispatch("dummy", params, ctx)
    assert result == {"subject_id": "admin", "tenant_id": "default"}


class TestFastAPIServerAuth:
    """Test FastAPI server authentication."""

    @pytest.mark.asyncio
    async def test_get_auth_result_with_database_auth(self):
        """Test auth with database auth provider."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        mock_auth_provider = MagicMock()
        # Auth provider returns an object with attributes, not a dict
        auth_result = SimpleNamespace(
            authenticated=True,
            subject_type="user",
            subject_id="alice",
            tenant_id="default",
            is_admin=False,
            inherit_permissions=True,
        )
        mock_auth_provider.authenticate = AsyncMock(return_value=auth_result)

        fas._app_state.auth_provider = mock_auth_provider

        auth = await fas.get_auth_result(
            authorization="Bearer sk-test-key",
            x_agent_id=None,
            x_nexus_subject=None,
            x_nexus_tenant_id=None,
        )

        assert auth["authenticated"] is True
        assert auth["subject_id"] == "alice"
        mock_auth_provider.authenticate.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_auth_result_with_x_agent_id(self):
        """Test auth with X-Agent-ID header."""
        fas._app_state.api_key = None
        fas._app_state.auth_provider = None

        token = "sk-default_admin_deadbeef_0123456789abcdef0123456789abcdef"
        auth = await fas.get_auth_result(
            authorization=f"Bearer {token}",
            x_agent_id="agent-123",
            x_nexus_subject=None,
            x_nexus_tenant_id=None,
        )

        assert auth["authenticated"] is True
        assert auth["subject_id"] == "admin"
        assert auth.get("x_agent_id") == "agent-123"  # Stored as x_agent_id, not in metadata

    @pytest.mark.asyncio
    async def test_get_auth_result_no_authorization(self):
        """Test auth without authorization header."""
        fas._app_state.api_key = None
        fas._app_state.auth_provider = None

        auth = await fas.get_auth_result(
            authorization=None,
            x_agent_id=None,
            x_nexus_subject=None,
            x_nexus_tenant_id=None,
        )

        # In open access mode (no auth configured), it returns authenticated=True
        # with open_access metadata
        assert auth is not None
        assert auth["authenticated"] is True
        assert auth["metadata"]["open_access"] is True

    @pytest.mark.asyncio
    async def test_get_auth_result_invalid_token_format(self):
        """Test auth with invalid token format."""
        fas._app_state.api_key = None
        fas._app_state.auth_provider = None

        auth = await fas.get_auth_result(
            authorization="Bearer invalid-token-format",
            x_agent_id=None,
            x_nexus_subject=None,
            x_nexus_tenant_id=None,
        )

        # Should still try to infer from token
        assert auth is not None


class TestFastAPIServerHandlers:
    """Test FastAPI server handler functions."""

    def test_handle_read(self):
        """Test read handler."""

        class FS:
            def read(self, path: str, return_metadata: bool = False, context=None):
                # Read handler returns raw bytes, encode_rpc_message will wrap it
                return b"data"

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(path="/test.txt", return_metadata=False)

        result = fas._handle_read(params, ctx)

        # Read handler returns raw bytes
        assert result == b"data"

    def test_handle_read_with_metadata(self):
        """Test read handler with metadata."""

        class FS:
            def read(self, path: str, return_metadata: bool = False, context=None):
                return {"content": b"data", "size": 4, "etag": "etag123"}

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(path="/test.txt", return_metadata=True)

        result = fas._handle_read(params, ctx)

        assert result["content"] == b"data"
        assert result["size"] == 4

    def test_handle_write(self):
        """Test write handler."""

        class FS:
            def write(
                self,
                path: str,
                content: bytes,
                if_match: str | None = None,
                if_none_match: bool = False,
                force: bool = False,
                context=None,
            ):
                # Write returns dict with metadata, handler wraps it
                return {"etag": "etag123", "size": len(content)}

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(
            path="/test.txt", content=b"hello", if_match=None, if_none_match=False, force=False
        )

        result = fas._handle_write(params, ctx)

        # Handler wraps the dict return value in {"bytes_written": <dict>}
        # The write() method returns a dict, and handler assigns it to bytes_written
        assert "bytes_written" in result
        # The bytes_written value is the entire dict returned by write()
        assert isinstance(result["bytes_written"], dict)
        assert result["bytes_written"]["size"] == 5

    def test_handle_write_with_if_match(self):
        """Test write handler with if_match (etag)."""

        class FS:
            def write(
                self,
                path: str,
                content: bytes,
                if_match: str | None = None,
                if_none_match: bool = False,
                force: bool = False,
                context=None,
            ):
                if if_match == "old-etag":
                    from nexus.core.exceptions import ConflictError

                    raise ConflictError(path, "old-etag", "new-etag")
                return {"etag": "new-etag", "size": len(content)}

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(
            path="/test.txt",
            content=b"hello",
            if_match="old-etag",
            if_none_match=False,
            force=False,
        )

        with pytest.raises(ConflictError):
            fas._handle_write(params, ctx)

    def test_handle_list(self):
        """Test list handler."""

        class FS:
            def list(
                self,
                path: str = "/",
                recursive: bool = True,
                details: bool = False,
                prefix: str | None = None,
                show_parsed: bool = True,
                context=None,
            ):
                return ["/file1.txt", "/file2.txt"]

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(
            path="/workspace", recursive=True, details=False, prefix=None, show_parsed=True
        )

        result = fas._handle_list(params, ctx)

        assert result == {"files": ["/file1.txt", "/file2.txt"]}

    @pytest.mark.asyncio
    async def test_handle_stat(self):
        """Test stat handler - stat is handled via _auto_dispatch."""

        # Stat is not a separate handler, it's handled via auto_dispatch
        # So we test it through the exposed methods
        class FS:
            def stat(self, path: str, context=None):
                return {"size": 1024, "is_directory": False, "etag": "etag123"}

        fs = FS()
        fas._app_state.nexus_fs = fs
        fas._app_state.exposed_methods = {"stat": fs.stat}

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(path="/test.txt")

        result = await fas._auto_dispatch("stat", params, ctx)

        assert result["size"] == 1024
        assert result["is_directory"] is False

    def test_handle_exists(self):
        """Test exists handler."""

        class FS:
            def exists(self, path: str, context=None):
                return True

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(path="/test.txt")

        result = fas._handle_exists(params, ctx)

        assert result == {"exists": True}

    def test_handle_mkdir(self):
        """Test mkdir handler."""

        class FS:
            def mkdir(self, path: str, context=None):
                pass

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(path="/newdir")

        result = fas._handle_mkdir(params, ctx)

        assert result == {"created": True}

    def test_handle_rmdir(self):
        """Test rmdir handler."""

        class FS:
            def rmdir(self, path: str, context=None):
                pass

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(path="/olddir")

        result = fas._handle_rmdir(params, ctx)

        assert result == {"removed": True}

    def test_handle_glob(self):
        """Test glob handler."""

        class FS:
            def glob(self, pattern: str, path: str = "/", context=None):
                return ["/file1.py", "/file2.py"]

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(pattern="*.py", path="/workspace")

        result = fas._handle_glob(params, ctx)

        assert "matches" in result
        assert result["matches"] == ["/file1.py", "/file2.py"]

    def test_handle_grep(self):
        """Test grep handler."""

        class FS:
            def grep(
                self,
                pattern: str,
                path: str = "/",
                file_pattern: str | None = None,
                ignore_case: bool = False,
                max_results: int = 1000,
                context=None,
            ):
                return [{"file": "/test.py", "line": 10, "content": "def test():"}]

        fs = FS()
        fas._app_state.nexus_fs = fs

        ctx = OperationContext(
            user="admin",
            groups=[],
            subject_type="user",
            subject_id="admin",
            tenant_id="default",
            is_admin=True,
        )
        params = SimpleNamespace(
            pattern="def test",
            path="/workspace",
            file_pattern="*.py",
            ignore_case=False,
            max_results=100,
        )

        result = fas._handle_grep(params, ctx)

        assert "results" in result
        assert len(result["results"]) == 1
        assert result["results"][0]["file"] == "/test.py"
