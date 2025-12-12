"""Unit tests for FastAPI server auth/context behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
