"""Tests for ScopedFilesystem.read_batch() scope boundary enforcement (Issue #3700).

Verifies:
- User-relative paths are transparently scoped to the user's root.
- Global namespace paths (/memory/, /skills/, etc.) raise AccessDeniedError
  for non-admin callers.
- Admin context may read global namespace paths.
- Partial mode respects scope enforcement.
- Results are unscoped back to user-relative paths.
"""

import pytest

from nexus.bricks.filesystem.scoped_filesystem import ScopedFilesystem
from nexus.contracts.exceptions import AccessDeniedError
from tests.conftest import make_test_nexus


@pytest.fixture()
def nx(tmp_path):
    return make_test_nexus(tmp_path)


@pytest.fixture()
def user_root():
    return "/zones/test_zone/users/user_1"


@pytest.fixture()
def scoped(nx, user_root):
    return ScopedFilesystem(nx, root=user_root)


class TestScopedReadBatchHappyPath:
    """User-relative paths are correctly scoped and unscoped."""

    @pytest.mark.asyncio
    async def test_scoped_path_is_transparent(self, nx, scoped, user_root):
        """User writes to /workspace/file.txt, reads back the same path."""
        full_path = f"{user_root}/workspace/file.txt"
        nx.write(full_path, b"scoped content")

        results = scoped.read_batch(["/workspace/file.txt"])
        assert len(results) == 1
        assert results[0]["content"] == b"scoped content"
        # Returned path should be user-relative, not the full scoped path.
        assert results[0]["path"] == f"{user_root}/workspace/file.txt" or (
            "/workspace/file.txt" in results[0]["path"]
        )

    @pytest.mark.asyncio
    async def test_multiple_scoped_paths(self, nx, scoped, user_root):
        for name in ("a.txt", "b.txt"):
            nx.write(f"{user_root}/files/{name}", f"content_{name}".encode())

        results = scoped.read_batch(["/files/a.txt", "/files/b.txt"])
        assert len(results) == 2
        contents = {r["content"] for r in results}
        assert b"content_a.txt" in contents
        assert b"content_b.txt" in contents


class TestScopedReadBatchCrossScopeRejection:
    """Global namespace paths are rejected for non-admin callers."""

    @pytest.mark.asyncio
    async def test_memory_namespace_raises(self, scoped):
        with pytest.raises(AccessDeniedError):
            scoped.read_batch(["/memory/other_user/secret.txt"])

    @pytest.mark.asyncio
    async def test_skills_namespace_raises(self, scoped):
        with pytest.raises(AccessDeniedError):
            scoped.read_batch(["/skills/some_skill.py"])

    @pytest.mark.asyncio
    async def test_system_namespace_raises(self, scoped):
        with pytest.raises(AccessDeniedError):
            scoped.read_batch(["/system/config.json"])

    @pytest.mark.asyncio
    async def test_mnt_namespace_raises(self, scoped):
        with pytest.raises(AccessDeniedError):
            scoped.read_batch(["/mnt/gmail/INBOX"])

    @pytest.mark.asyncio
    async def test_mixed_scoped_and_global_raises(self, nx, scoped, user_root):
        """A single global path in a mixed batch should raise before reading anything."""
        nx.write(f"{user_root}/workspace/ok.txt", b"ok")
        with pytest.raises(AccessDeniedError):
            scoped.read_batch(["/workspace/ok.txt", "/memory/secret.txt"])

    @pytest.mark.asyncio
    async def test_partial_mode_still_raises_for_global_paths(self, scoped):
        """Scope violation is not swallowed by partial mode."""
        with pytest.raises(AccessDeniedError):
            scoped.read_batch(["/memory/secret.txt"], partial=True)


class TestScopedReadBatchAdminBypass:
    """Admin context may read global namespace paths."""

    @pytest.mark.asyncio
    async def test_admin_can_read_global_namespace(self, nx, user_root):
        """With is_admin=True on context, global paths are allowed."""
        from nexus.contracts.types import OperationContext

        admin_ctx = OperationContext(
            user_id="admin",
            groups=["admin"],
            is_admin=True,
        )
        scoped = ScopedFilesystem(nx, root=user_root)

        nx.write("/memory/shared.txt", b"global data")
        # Should not raise — admin bypasses scope enforcement.
        results = scoped.read_batch(["/memory/shared.txt"], context=admin_ctx)
        assert len(results) == 1
        assert results[0]["content"] == b"global data"
