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
    router.add_mount("/workspace", temp_backend)

    result = router.route("/workspace")

    assert result.backend == temp_backend
    assert result.backend_path == ""
    assert result.mount_point == "/workspace"
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
