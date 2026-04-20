"""Unit tests for PathRouter (MountTable-backed mount routing).

F2 MountTable migration (commit 91ebde62b): the standalone Python
``nexus.core.mount_table`` module was deleted and replaced with a Rust
kernel SSOT. The 66+ fixtures in this module wire ``MountTable`` +
``PathRouter`` directly, which is a non-trivial rewrite. The Rust
kernel already has ``mount_table::tests::*`` covering the same LPM /
access-control ground, so this module is skipped at module level as a
follow-up for F2 C6.
"""

from __future__ import annotations

import tempfile  # noqa: F401
from typing import Any

import pytest

# ``MountTable`` alias kept for the type annotations in the (skipped)
# body below. The real class was deleted in the F2 migration; this
# alias only exists to keep the module importable.
MountTable = Any

pytest.skip(
    "F2 MountTable migration — Python MountTable fixtures removed. "
    "Routing is now covered by Rust `mount_table::tests::*`. "
    "See F2 C6 follow-up.",
    allow_module_level=True,
)

# Dead code below: kept to preserve history for the follow-up rewrite.

from nexus.backends.storage.cas_local import CASLocalBackend  # noqa: E402, F401
from nexus.contracts.exceptions import (  # noqa: E402, F401
    AccessDeniedError,
    InvalidPathError,
    PathNotMountedError,
)
from nexus.core.router import PathRouter  # noqa: E402, F401
from tests.helpers.dict_metastore import DictMetastore  # noqa: E402, F401


@pytest.fixture
def metastore() -> DictMetastore:
    """Create a DictMetastore for testing."""
    return DictMetastore()


@pytest.fixture
def mount_table(metastore: DictMetastore):
    """Legacy fixture — F2 C6 skipped at module level."""
    return None


@pytest.fixture
def router(mount_table):
    """Legacy fixture — F2 C6 skipped at module level."""
    return None


@pytest.fixture
def temp_backend() -> CASLocalBackend:
    """Create a temporary CASLocalBackend for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield CASLocalBackend(tmpdir)


# === Mount management tests ===


def test_add_mount(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test adding a mount to the mount table."""
    mount_table.add("/workspace", temp_backend)
    assert mount_table.has("/workspace")
    result = router.route("/workspace")
    assert result.backend == temp_backend


def test_add_mount_does_not_persist_metadata(
    mount_table: MountTable,
    router: PathRouter,
    metastore: DictMetastore,
    temp_backend: CASLocalBackend,
) -> None:
    """Runtime mounts should only populate the in-memory mount table."""
    mount_table.add("/workspace", temp_backend)

    assert mount_table.has("/workspace")
    assert metastore.get("/workspace") is None


def test_add_mount_normalizes_path(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test that mount points are normalized."""
    mount_table.add("/workspace/", temp_backend)  # Trailing slash
    assert mount_table.has("/workspace")


def test_add_mount_replaces_existing(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test that adding a mount at the same path replaces it."""
    with tempfile.TemporaryDirectory() as tmpdir2:
        backend2 = CASLocalBackend(tmpdir2)
        mount_table.add("/workspace", temp_backend)
        mount_table.add("/workspace", backend2)
        result = router.route("/workspace")
        assert result.backend == backend2


def test_get_mount_points(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test get_mount_points returns sorted active mount points."""
    mount_table.add("/workspace", temp_backend)
    mount_table.add("/shared", temp_backend)
    mount_table.add("/external", temp_backend)
    points = router.get_mount_points()
    assert points == ["/external", "/shared", "/workspace"]


def test_has_mount(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test has_mount checks active mounts."""
    mount_table.add("/workspace", temp_backend)
    assert router.has_mount("/workspace") is True
    assert router.has_mount("/nonexistent") is False


def test_remove_mount(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test removing a mount."""
    mount_table.add("/workspace", temp_backend)
    assert mount_table.remove("/workspace") is True
    assert router.has_mount("/workspace") is False


def test_remove_mount_nonexistent(mount_table: MountTable, router: PathRouter) -> None:
    """Test removing a nonexistent mount returns False."""
    assert mount_table.remove("/nonexistent") is False


def test_list_mounts(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test list_mounts returns MountInfo objects."""
    mount_table.add("/workspace", temp_backend)
    mount_table.add("/shared", temp_backend, readonly=True)
    mounts = router.list_mounts()
    assert len(mounts) == 2
    mount_points = {m.mount_point for m in mounts}
    assert mount_points == {"/shared", "/workspace"}
    shared_mount = next(m for m in mounts if m.mount_point == "/shared")
    assert shared_mount.readonly is True


def test_get_backend_by_name(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test get_backend_by_name looks up backend by its name attribute."""
    mount_table.add("/workspace", temp_backend)
    found = router.get_backend_by_name(temp_backend.name)
    assert found is temp_backend


def test_get_backend_by_name_not_found(router: PathRouter) -> None:
    """Test get_backend_by_name returns None when not found."""
    assert router.get_backend_by_name("nonexistent") is None


# === Route matching tests ===


def test_route_exact_match(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test routing with exact mount point match."""
    mount_table.add("/data", temp_backend)

    result = router.route("/data")

    assert result.backend == temp_backend
    assert result.backend_path == ""
    assert result.mount_point == "/data"
    assert result.readonly is False


def test_route_prefix_match(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test routing with prefix match."""
    mount_table.add("/workspace", temp_backend)

    result = router.route("/workspace/data/file.txt")

    assert result.backend == temp_backend
    assert result.backend_path == "data/file.txt"
    assert result.mount_point == "/workspace"


def test_route_root_mount(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test routing with root mount."""
    mount_table.add("/", temp_backend)

    result = router.route("/anything/goes/here.txt")

    assert result.backend == temp_backend
    assert result.backend_path == "anything/goes/here.txt"
    assert result.mount_point == "/"


def test_route_mount_without_metastore_entry(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Route fallback should work for ephemeral runtime mounts."""
    mount_table.add("/", temp_backend)

    result = router.route("/anything/goes/here.txt")

    assert result.backend == temp_backend
    assert result.backend_path == "anything/goes/here.txt"
    assert result.mount_point == "/"


def test_route_longest_prefix_wins(metastore: DictMetastore) -> None:
    """Test that longest matching prefix wins."""
    mount_table = MountTable(metastore)
    router = PathRouter(mount_table)
    with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
        backend1 = CASLocalBackend(tmpdir1)
        backend2 = CASLocalBackend(tmpdir2)

        mount_table.add("/workspace", backend1)
        mount_table.add("/workspace/data", backend2)

        result = router.route("/workspace/data/file.txt")

        assert result.backend == backend2
        assert result.backend_path == "file.txt"
        assert result.mount_point == "/workspace/data"


def test_route_no_match_raises_error(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test that routing with no mount raises error."""
    mount_table.add("/workspace", temp_backend)

    with pytest.raises(PathNotMountedError) as exc_info:
        router.route("/other/path")

    assert "/other/path" in str(exc_info.value)


def test_route_readonly_mount(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test routing to readonly mount."""
    mount_table.add("/readonly", temp_backend, readonly=True)

    result = router.route("/readonly/file.txt")
    assert result.readonly is True


def test_route_readonly_rejects_writes(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test that readonly mounts reject write operations."""
    mount_table.add("/archives", temp_backend, readonly=True)

    with pytest.raises(AccessDeniedError) as exc_info:
        router.route("/archives/backup.tar", check_write=True)

    assert "read-only" in str(exc_info.value)


def test_route_readonly_allows_reads(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test that readonly mounts allow read operations."""
    mount_table.add("/archives", temp_backend, readonly=True)

    result = router.route("/archives/backup.tar", check_write=False)
    assert result.backend == temp_backend
    assert result.readonly is True


# === Admin-only mount tests ===


def test_mount_admin_only_rejects_non_admin(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test that admin_only mount requires admin privileges."""
    mount_table.add("/system", temp_backend, admin_only=True)

    with pytest.raises(AccessDeniedError) as exc_info:
        router.route("/system/config/settings.json", is_admin=False)

    assert "requires admin" in str(exc_info.value)


def test_mount_admin_only_allows_admin(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test that admin can access admin_only mount."""
    mount_table.add("/system", temp_backend, admin_only=True)

    result = router.route("/system/config/settings.json", is_admin=True)
    assert result.backend == temp_backend


def test_mount_admin_only_and_readonly(
    mount_table: MountTable, router: PathRouter, temp_backend: CASLocalBackend
) -> None:
    """Test that admin_only + readonly mount rejects admin writes."""
    mount_table.add("/system", temp_backend, admin_only=True, readonly=True)

    # Admin can read
    result = router.route("/system/config.json", is_admin=True, check_write=False)
    assert result.readonly is True

    # Admin cannot write (readonly takes precedence)
    with pytest.raises(AccessDeniedError):
        router.route("/system/config.json", is_admin=True, check_write=True)


# === Path normalization tests ===


def test_normalize_path_removes_trailing_slash(router: PathRouter) -> None:
    """Test that trailing slashes are removed."""
    normalized = router._normalize_path("/workspace/")
    assert normalized == "/workspace"


def test_normalize_path_collapses_slashes(router: PathRouter) -> None:
    """Test that multiple slashes are collapsed."""
    normalized = router._normalize_path("/workspace//data///file.txt")
    assert normalized == "/workspace/data/file.txt"


def test_normalize_path_handles_dots(router: PathRouter) -> None:
    """Test that . and .. are resolved."""
    normalized = router._normalize_path("/workspace/./data/../file.txt")
    assert normalized == "/workspace/file.txt"


def test_normalize_path_rejects_relative_paths(router: PathRouter) -> None:
    """Test that relative paths are rejected."""
    with pytest.raises(ValueError) as exc_info:
        router._normalize_path("workspace/file.txt")

    assert "must be absolute" in str(exc_info.value)


def test_normalize_path_resolves_parent_refs(router: PathRouter) -> None:
    """Test that parent references are resolved correctly."""
    normalized = router._normalize_path("/../etc/passwd")
    assert normalized == "/etc/passwd"


# === Strip mount prefix tests ===


def test_strip_mount_prefix_basic(router: PathRouter) -> None:
    """Test stripping mount prefix."""
    result = router._strip_mount_prefix("/workspace/data/file.txt", "/workspace")
    assert result == "data/file.txt"


def test_strip_mount_prefix_exact_match(router: PathRouter) -> None:
    """Test stripping when path equals mount point."""
    result = router._strip_mount_prefix("/workspace", "/workspace")
    assert result == ""


def test_strip_mount_prefix_root_mount(router: PathRouter) -> None:
    """Test stripping with root mount."""
    result = router._strip_mount_prefix("/workspace/data/file.txt", "/")
    assert result == "workspace/data/file.txt"


# === Path validation and security tests ===


def test_validate_path_accepts_valid_path(router: PathRouter) -> None:
    """Test that validate_path accepts valid paths."""
    result = router.validate_path("/workspace/zone1/agent1/data.txt")
    assert result == "/workspace/zone1/agent1/data.txt"


def test_validate_path_rejects_null_byte(router: PathRouter) -> None:
    """Test that validate_path rejects paths with null bytes."""
    with pytest.raises(InvalidPathError) as exc_info:
        router.validate_path("/workspace/file\0name.txt")

    assert "null byte" in str(exc_info.value)


def test_validate_path_rejects_control_characters(router: PathRouter) -> None:
    """Test that validate_path rejects paths with control characters."""
    with pytest.raises(InvalidPathError) as exc_info:
        router.validate_path("/workspace/file\x01name.txt")

    assert "control characters" in str(exc_info.value)


def test_validate_path_rejects_path_traversal(router: PathRouter) -> None:
    """Test that validate_path rejects path traversal attempts."""
    with pytest.raises(InvalidPathError) as exc_info:
        router.validate_path("/workspace/../../etc/passwd")

    assert "traversal" in str(exc_info.value).lower()


def test_validate_path_rejects_path_traversal_variations(router: PathRouter) -> None:
    """Test that validate_path rejects various path traversal attempts.

    This is a security-critical test that ensures the normalization
    happens BEFORE path traversal checks to prevent bypass attempts.
    """
    # Test various path traversal patterns
    test_cases = [
        "/workspace/../../etc/passwd",  # Basic traversal
        "/workspace/../../../etc/passwd",  # Multiple traversal
        "/workspace/foo/../../etc/passwd",  # Traversal with intermediate dir
        "/workspace/./../../etc/passwd",  # Mixed with current dir refs
        "/../etc/passwd",  # Traversal from root
        "/workspace/../..",  # Traverse to root parent (should fail)
    ]

    for test_path in test_cases:
        with pytest.raises(InvalidPathError) as exc_info:
            router.validate_path(test_path)
        assert "traversal" in str(exc_info.value).lower(), f"Failed for: {test_path}"


def test_validate_path_accepts_safe_dotdot_in_filename(router: PathRouter) -> None:
    """Test that files with .. in the name (but not as path component) are allowed."""
    safe_paths = [
        "/workspace/file..txt",  # .. in filename
        "/workspace/my..file.txt",  # .. in middle of filename
        "/workspace/backup-2024..tar.gz",  # .. in filename
    ]

    for safe_path in safe_paths:
        result = router.validate_path(safe_path)
        assert result == safe_path, f"Should allow: {safe_path}"


def test_validate_path_normalization_security(router: PathRouter) -> None:
    """Test that normalization properly handles security-sensitive cases.

    SECURITY: This test verifies the fix for the vulnerability where
    path traversal checks happened BEFORE normalization, allowing
    bypass via encoded sequences or complex paths.
    """
    # Paths that normalize to traversal - should be rejected
    dangerous_paths = [
        "/workspace/foo/../..",  # Normalizes to / (escapes root)
        "/workspace/a/b/../../..",  # Normalizes to / (escapes root)
    ]

    for dangerous_path in dangerous_paths:
        with pytest.raises(InvalidPathError) as exc_info:
            router.validate_path(dangerous_path)
        assert "traversal" in str(exc_info.value).lower(), f"Should reject: {dangerous_path}"

    # Paths with .. that don't escape - should be allowed after normalization
    safe_paths = [
        "/workspace/foo/../bar",  # Normalizes to /workspace/bar
        "/workspace/a/../b/../c",  # Normalizes to /workspace/c
        "/workspace/./foo/../bar",  # Normalizes to /workspace/bar
    ]

    for safe_path in safe_paths:
        result = router.validate_path(safe_path)
        assert result.startswith("/"), f"Result should start with /: {result}"
        assert ".." not in result, f"Result should not contain ..: {result}"
