"""Unit tests for PathRouter."""

import tempfile

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.router import PathNotMountedError, PathRouter


@pytest.fixture
def router() -> PathRouter:
    """Create a PathRouter instance."""
    return PathRouter()


@pytest.fixture
def temp_backend() -> LocalBackend:
    """Create a temporary LocalBackend for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield LocalBackend(tmpdir)


def test_add_mount(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test adding a mount to the router."""
    router.add_mount("/workspace", temp_backend)
    assert len(router._mounts) == 1
    assert router._mounts[0].mount_point == "/workspace"
    assert router._mounts[0].backend == temp_backend


def test_add_mount_normalizes_path(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test that mount points are normalized."""
    router.add_mount("/workspace/", temp_backend)  # Trailing slash
    assert router._mounts[0].mount_point == "/workspace"


def test_add_mount_sorts_by_priority(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test that mounts are sorted by priority."""
    router.add_mount("/low", temp_backend, priority=0)
    router.add_mount("/high", temp_backend, priority=10)
    router.add_mount("/medium", temp_backend, priority=5)

    assert router._mounts[0].mount_point == "/high"
    assert router._mounts[1].mount_point == "/medium"
    assert router._mounts[2].mount_point == "/low"


def test_add_mount_sorts_by_length_when_priority_equal(
    router: PathRouter, temp_backend: LocalBackend
) -> None:
    """Test that longer prefixes come first when priorities are equal."""
    router.add_mount("/workspace", temp_backend, priority=0)
    router.add_mount("/workspace/data", temp_backend, priority=0)

    # Longer prefix should come first
    assert router._mounts[0].mount_point == "/workspace/data"
    assert router._mounts[1].mount_point == "/workspace"


def test_route_exact_match(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test routing with exact mount point match."""
    router.add_mount("/data", temp_backend)

    result = router.route("/data")

    assert result.backend == temp_backend
    assert result.backend_path == ""
    assert result.mount_point == "/data"
    assert result.readonly is False


def test_route_prefix_match(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test routing with prefix match."""
    router.add_mount("/workspace", temp_backend)

    result = router.route("/workspace/data/file.txt")

    assert result.backend == temp_backend
    assert result.backend_path == "data/file.txt"
    assert result.mount_point == "/workspace"


def test_route_root_mount(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test routing with root mount."""
    router.add_mount("/", temp_backend)

    result = router.route("/anything/goes/here.txt")

    assert result.backend == temp_backend
    assert result.backend_path == "anything/goes/here.txt"
    assert result.mount_point == "/"


def test_route_longest_prefix_wins(router: PathRouter) -> None:
    """Test that longest matching prefix wins."""
    with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
        backend1 = LocalBackend(tmpdir1)
        backend2 = LocalBackend(tmpdir2)

        router.add_mount("/workspace", backend1)
        router.add_mount("/workspace/data", backend2)

        result = router.route("/workspace/data/file.txt")

        assert result.backend == backend2
        assert result.backend_path == "file.txt"
        assert result.mount_point == "/workspace/data"


def test_route_no_match_raises_error(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test that routing with no mount raises error."""
    router.add_mount("/workspace", temp_backend)

    with pytest.raises(PathNotMountedError) as exc_info:
        router.route("/other/path")

    assert "/other/path" in str(exc_info.value)


def test_route_readonly_mount(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test routing to readonly mount."""
    router.add_mount("/readonly", temp_backend, readonly=True)

    result = router.route("/readonly/file.txt")

    assert result.readonly is True


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
    # posixpath.normpath resolves .. but keeps absolute paths
    # "/../etc/passwd" becomes "/etc/passwd" which is valid
    normalized = router._normalize_path("/../etc/passwd")
    assert normalized == "/etc/passwd"


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


def test_match_longest_prefix_exact(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test matching exact mount point."""
    router.add_mount("/workspace", temp_backend)

    match = router._match_longest_prefix("/workspace")

    assert match is not None
    assert match.mount_point == "/workspace"


def test_match_longest_prefix_subdirectory(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test matching subdirectory."""
    router.add_mount("/workspace", temp_backend)

    match = router._match_longest_prefix("/workspace/data/file.txt")

    assert match is not None
    assert match.mount_point == "/workspace"


def test_match_longest_prefix_no_match(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test no match returns None."""
    router.add_mount("/workspace", temp_backend)

    match = router._match_longest_prefix("/other/path")

    assert match is None


def test_match_longest_prefix_root_matches_all(
    router: PathRouter, temp_backend: LocalBackend
) -> None:
    """Test that root mount matches everything."""
    router.add_mount("/", temp_backend)

    match = router._match_longest_prefix("/anything/goes/here")

    assert match is not None
    assert match.mount_point == "/"


def test_match_prevents_false_prefix(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test that /workspace doesn't match /workspace2."""
    router.add_mount("/workspace", temp_backend)

    match = router._match_longest_prefix("/workspace2/file.txt")

    assert match is None


# === Namespace and Access Control Tests ===


def test_validate_path_accepts_valid_path(router: PathRouter) -> None:
    """Test that validate_path accepts valid paths."""
    result = router.validate_path("/workspace/tenant1/agent1/data.txt")
    assert result == "/workspace/tenant1/agent1/data.txt"


def test_validate_path_rejects_null_byte(router: PathRouter) -> None:
    """Test that validate_path rejects paths with null bytes."""
    from nexus.core.router import InvalidPathError

    with pytest.raises(InvalidPathError) as exc_info:
        router.validate_path("/workspace/file\0name.txt")

    assert "null byte" in str(exc_info.value)


def test_validate_path_rejects_control_characters(router: PathRouter) -> None:
    """Test that validate_path rejects paths with control characters."""
    from nexus.core.router import InvalidPathError

    with pytest.raises(InvalidPathError) as exc_info:
        router.validate_path("/workspace/file\x01name.txt")

    assert "control characters" in str(exc_info.value)


def test_validate_path_rejects_path_traversal(router: PathRouter) -> None:
    """Test that validate_path rejects path traversal attempts."""
    from nexus.core.router import InvalidPathError

    with pytest.raises(InvalidPathError) as exc_info:
        router.validate_path("/workspace/../../etc/passwd")

    assert "traversal" in str(exc_info.value).lower()


def test_parse_path_workspace(router: PathRouter) -> None:
    """Test parsing workspace namespace path."""
    path_info = router.parse_path("/workspace/acme/agent1/data/file.txt")

    assert path_info.namespace == "workspace"
    assert path_info.tenant_id == "acme"
    assert path_info.agent_id == "agent1"
    assert path_info.relative_path == "data/file.txt"


def test_parse_path_shared(router: PathRouter) -> None:
    """Test parsing shared namespace path."""
    path_info = router.parse_path("/shared/acme/datasets/model.pkl")

    assert path_info.namespace == "shared"
    assert path_info.tenant_id == "acme"
    assert path_info.agent_id is None
    assert path_info.relative_path == "datasets/model.pkl"


def test_parse_path_archives(router: PathRouter) -> None:
    """Test parsing archives namespace path."""
    path_info = router.parse_path("/archives/acme/2024/01/backup.tar")

    assert path_info.namespace == "archives"
    assert path_info.tenant_id == "acme"
    assert path_info.agent_id is None
    assert path_info.relative_path == "2024/01/backup.tar"


def test_parse_path_external(router: PathRouter) -> None:
    """Test parsing external namespace path."""
    path_info = router.parse_path("/external/s3/bucket/file.txt")

    assert path_info.namespace == "external"
    assert path_info.tenant_id is None
    assert path_info.agent_id is None
    assert path_info.relative_path == "s3/bucket/file.txt"


def test_parse_path_system(router: PathRouter) -> None:
    """Test parsing system namespace path."""
    path_info = router.parse_path("/system/config/settings.json")

    assert path_info.namespace == "system"
    assert path_info.tenant_id is None
    assert path_info.agent_id is None
    assert path_info.relative_path == "config/settings.json"


def test_parse_path_workspace_partial_paths(router: PathRouter) -> None:
    """Test that workspace allows partial paths for directory creation."""
    # /workspace - just namespace
    path_info = router.parse_path("/workspace")
    assert path_info.namespace == "workspace"
    assert path_info.tenant_id is None
    assert path_info.agent_id is None

    # /workspace/tenant1 - namespace + tenant
    path_info = router.parse_path("/workspace/tenant1")
    assert path_info.namespace == "workspace"
    assert path_info.tenant_id == "tenant1"
    assert path_info.agent_id is None


def test_parse_path_shared_partial_paths(router: PathRouter) -> None:
    """Test that shared allows partial paths for directory creation."""
    # /shared - just namespace
    path_info = router.parse_path("/shared")
    assert path_info.namespace == "shared"
    assert path_info.tenant_id is None

    # /shared/tenant1 - namespace + tenant
    path_info = router.parse_path("/shared/tenant1")
    assert path_info.namespace == "shared"
    assert path_info.tenant_id == "tenant1"


def test_namespace_configuration_defaults(router: PathRouter) -> None:
    """Test that default namespaces are registered."""
    assert "workspace" in router._namespaces
    assert "shared" in router._namespaces
    assert "external" in router._namespaces
    assert "system" in router._namespaces
    assert "archives" in router._namespaces


def test_namespace_system_is_readonly(router: PathRouter) -> None:
    """Test that system namespace is read-only."""
    assert router._namespaces["system"].readonly is True


def test_namespace_system_is_admin_only(router: PathRouter) -> None:
    """Test that system namespace is admin-only."""
    assert router._namespaces["system"].admin_only is True


def test_namespace_archives_is_readonly(router: PathRouter) -> None:
    """Test that archives namespace is read-only."""
    assert router._namespaces["archives"].readonly is True


def test_route_with_tenant_isolation(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test routing with tenant isolation enforced."""
    router.add_mount("/workspace", temp_backend)

    # Should succeed - matching tenant
    result = router.route("/workspace/acme/agent1/data.txt", tenant_id="acme")
    assert result.backend == temp_backend
    assert result.backend_path == "acme/agent1/data.txt"


def test_route_with_tenant_mismatch_raises_error(
    router: PathRouter, temp_backend: LocalBackend
) -> None:
    """Test that tenant mismatch raises AccessDeniedError."""
    from nexus.core.router import AccessDeniedError

    router.add_mount("/workspace", temp_backend)

    with pytest.raises(AccessDeniedError) as exc_info:
        router.route("/workspace/acme/agent1/data.txt", tenant_id="other_tenant")

    assert "cannot access" in str(exc_info.value)


def test_route_admin_can_access_any_tenant(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test that admin can access any tenant's resources."""
    router.add_mount("/workspace", temp_backend)

    # Admin can access other tenant's resources
    result = router.route(
        "/workspace/acme/agent1/data.txt", tenant_id="other_tenant", is_admin=True
    )
    assert result.backend == temp_backend


def test_route_system_requires_admin(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test that system namespace requires admin privileges."""
    from nexus.core.router import AccessDeniedError

    router.add_mount("/system", temp_backend)

    with pytest.raises(AccessDeniedError) as exc_info:
        router.route("/system/config/settings.json", is_admin=False)

    assert "requires admin" in str(exc_info.value)


def test_route_system_allows_admin(router: PathRouter, temp_backend: LocalBackend) -> None:
    """Test that admin can access system namespace."""
    router.add_mount("/system", temp_backend)

    result = router.route("/system/config/settings.json", is_admin=True)
    assert result.backend == temp_backend


def test_route_readonly_namespace_rejects_writes(
    router: PathRouter, temp_backend: LocalBackend
) -> None:
    """Test that readonly namespaces reject write operations."""
    from nexus.core.router import AccessDeniedError

    router.add_mount("/archives", temp_backend)

    with pytest.raises(AccessDeniedError) as exc_info:
        router.route("/archives/acme/backup.tar", tenant_id="acme", check_write=True)

    assert "read-only" in str(exc_info.value)


def test_route_readonly_namespace_allows_reads(
    router: PathRouter, temp_backend: LocalBackend
) -> None:
    """Test that readonly namespaces allow read operations."""
    router.add_mount("/archives", temp_backend)

    # Should succeed - reading from readonly namespace
    result = router.route("/archives/acme/backup.tar", tenant_id="acme", check_write=False)
    assert result.backend == temp_backend
    assert result.readonly is True


def test_register_custom_namespace(router: PathRouter) -> None:
    """Test registering a custom namespace."""
    from nexus.core.router import NamespaceConfig

    custom_ns = NamespaceConfig(
        name="custom", readonly=False, admin_only=False, requires_tenant=False
    )
    router.register_namespace(custom_ns)

    assert "custom" in router._namespaces
    assert router._namespaces["custom"] == custom_ns


def test_route_without_tenant_id_allows_external(
    router: PathRouter, temp_backend: LocalBackend
) -> None:
    """Test that external namespace doesn't require tenant."""
    router.add_mount("/external", temp_backend)

    # Should succeed - external doesn't require tenant
    result = router.route("/external/s3/bucket/file.txt")
    assert result.backend == temp_backend
