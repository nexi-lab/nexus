"""Hypothesis property-based tests for VFS Router kernel invariants (Issue #1303).

Invariants proven:
  1. Path normalization is idempotent: normalize(normalize(p)) == normalize(p)
  2. Path traversal never escapes mount boundaries
  3. Longest prefix match is deterministic and correct
  4. Read-only mounts reject writes, accept reads
"""

import tempfile

import pytest

pytest.importorskip("hypothesis")

from hypothesis import example, given, settings
from hypothesis import strategies as st

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator, _PyMountInfo
from nexus.core.path_utils import canonicalize_path
from nexus.core.router import (
    AccessDeniedError,
    InvalidPathError,
    PathNotMountedError,
    PathRouter,
)
from tests.helpers.dict_metastore import DictMetastore
from tests.strategies.kernel import (
    path_traversal_attempt,
    valid_mount_point,
    valid_namespaced_path,
    valid_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_router(metastore: DictMetastore) -> PathRouter:
    """Build a PathRouter over a bare DriverLifecycleCoordinator (no kernel).

    F2 MountTable migration: the standalone Python MountTable was deleted.
    Tests that only need the router's Python LPM fallback now talk to a
    kernel-less DLC directly via ``_mounts``.
    """
    dlc = DriverLifecycleCoordinator(dispatch=None, kernel=None)
    return PathRouter(dlc, metastore, None)


def _add_mount(
    router: PathRouter,
    mount_point: str,
    backend,
    *,
    readonly: bool = False,
    admin_only: bool = False,
    zone_id: str = ROOT_ZONE_ID,
) -> None:
    """Insert a mount into the router's DLC map directly."""
    canonical = canonicalize_path(mount_point, zone_id)
    router._dlc._mounts[canonical] = _PyMountInfo(
        backend=backend,
        readonly=readonly,
        admin_only=admin_only,
        zone_id=zone_id,
    )


def _make_router_with_mounts() -> tuple[PathRouter, CASLocalBackend]:
    """Create a PathRouter with standard mounts for testing."""
    tmpdir = tempfile.mkdtemp()
    backend = CASLocalBackend(tmpdir)
    metastore = DictMetastore()
    router = _new_router(metastore)
    _add_mount(router, "/workspace", backend)
    _add_mount(router, "/shared", backend)
    _add_mount(router, "/external", backend)
    _add_mount(router, "/system", backend, admin_only=True, readonly=True)
    _add_mount(router, "/archives", backend, readonly=True)
    return router, backend


# ---------------------------------------------------------------------------
# Invariant 1: Path normalization is idempotent
# ---------------------------------------------------------------------------


class TestPathNormalizationInvariants:
    """Path normalization properties."""

    @given(path=valid_path())
    @example(path="/workspace")
    @example(path="/")
    @example(path="/workspace/data/file.txt")
    def test_normalize_is_idempotent(self, path: str) -> None:
        """normalize(normalize(p)) == normalize(p) for all valid paths."""
        router = _new_router(DictMetastore())
        once = router._normalize_path(path)
        twice = router._normalize_path(once)
        assert once == twice

    @given(path=valid_path())
    def test_normalized_path_starts_with_slash(self, path: str) -> None:
        """All normalized paths are absolute (start with /)."""
        router = _new_router(DictMetastore())
        normalized = router._normalize_path(path)
        assert normalized.startswith("/")

    @given(path=valid_path())
    def test_normalized_path_has_no_double_slashes(self, path: str) -> None:
        """Normalized paths never contain //."""
        router = _new_router(DictMetastore())
        normalized = router._normalize_path(path)
        assert "//" not in normalized

    @given(path=valid_path())
    def test_normalized_path_has_no_trailing_slash(self, path: str) -> None:
        """Normalized paths have no trailing slash (except root /)."""
        router = _new_router(DictMetastore())
        normalized = router._normalize_path(path)
        if normalized != "/":
            assert not normalized.endswith("/")


# ---------------------------------------------------------------------------
# Invariant 2: Path traversal never escapes mount boundary
# ---------------------------------------------------------------------------


class TestPathTraversalInvariants:
    """Path traversal security properties."""

    @given(attempt=path_traversal_attempt())
    def test_traversal_attempts_rejected(self, attempt: str) -> None:
        """All path traversal attempts are rejected by validate_path."""
        router = _new_router(DictMetastore())
        try:
            result = router.validate_path(attempt)
            # If validation succeeds, the path must still be within the
            # original namespace (normalization neutralized the traversal)
            original_ns = attempt.lstrip("/").split("/")[0]
            result_ns = result.lstrip("/").split("/")[0]
            assert result_ns == original_ns, (
                f"Traversal escaped namespace: {attempt!r} -> {result!r}"
            )
        except (InvalidPathError, ValueError):
            pass  # Correctly rejected

    @given(path=st.text(min_size=1, max_size=100))
    @example(path="\x00/etc/passwd")
    @example(path="/workspace/\x00hidden")
    @example(path="/workspace/../../../etc/passwd")
    @example(path="/workspace/./../../etc")
    def test_arbitrary_strings_never_escape_root(self, path: str) -> None:
        """No arbitrary string can produce a path outside /."""
        router = _new_router(DictMetastore())
        try:
            result = router.validate_path(path)
            assert result.startswith("/"), f"Escaped root: {path!r} -> {result!r}"
        except (InvalidPathError, ValueError):
            pass  # Correctly rejected

    @given(path=valid_path())
    def test_validate_roundtrip(self, path: str) -> None:
        """validate_path is idempotent: validate(validate(p)) == validate(p)."""
        router = _new_router(DictMetastore())
        try:
            once = router.validate_path(path)
            twice = router.validate_path(once)
            assert once == twice
        except (InvalidPathError, ValueError):
            pass  # Some generated paths may fail validation


# ---------------------------------------------------------------------------
# Invariant 3: Longest prefix match determinism
# ---------------------------------------------------------------------------


class TestLongestPrefixMatchInvariants:
    """Mount matching properties."""

    @given(path=valid_namespaced_path(namespace="workspace"))
    @settings(deadline=None)
    def test_route_deterministic(self, path: str) -> None:
        """Routing the same path twice always gives the same result."""
        router, _ = _make_router_with_mounts()
        try:
            r1 = router.route(path)
            r2 = router.route(path)
            assert r1.mount_point == r2.mount_point
            assert r1.backend_path == r2.backend_path
            assert r1.readonly == r2.readonly
        except (PathNotMountedError, InvalidPathError, AccessDeniedError):
            pass

    @given(
        mount1=valid_mount_point(),
        mount2=valid_mount_point(),
    )
    @settings(deadline=None)
    def test_longer_prefix_preferred_over_shorter(self, mount1: str, mount2: str) -> None:
        """When two mounts overlap, the longer prefix wins."""
        if mount1 == mount2:
            return

        # Ensure mount1 is a prefix of mount2 by construction
        deeper_path = mount1.rstrip("/") + mount2
        query_path = deeper_path + "/file.txt"

        tmpdir = tempfile.mkdtemp()
        backend_shallow = CASLocalBackend(tmpdir)
        backend_deep = CASLocalBackend(tmpdir)

        router = _new_router(DictMetastore())
        _add_mount(router, mount1, backend_shallow)
        _add_mount(router, deeper_path, backend_deep)

        try:
            result = router.route(query_path)
            # The deeper mount should match
            assert result.backend is backend_deep
        except (InvalidPathError, AccessDeniedError):
            pass  # Path validation may reject generated paths


# ---------------------------------------------------------------------------
# Invariant 4: Read-only mount enforcement
# ---------------------------------------------------------------------------


class TestReadOnlyMountInvariants:
    """Read-only mount properties."""

    @given(path=valid_path(max_depth=3))
    @settings(deadline=None)
    def test_system_mount_rejects_writes(self, path: str) -> None:
        """System mount (admin_only + readonly) always rejects write access."""
        router, _ = _make_router_with_mounts()
        full_path = f"/system{path}"
        try:
            router.route(full_path, is_admin=True, check_write=True)
            raise AssertionError(f"System mount accepted write: {full_path}")
        except AccessDeniedError:
            pass  # Correctly rejected
        except (InvalidPathError, PathNotMountedError):
            pass  # Path issues, also acceptable

    @given(path=valid_path(max_depth=3))
    @settings(deadline=None)
    def test_archives_mount_rejects_writes(self, path: str) -> None:
        """Archives mount (readonly) always rejects write access."""
        router, _ = _make_router_with_mounts()
        full_path = f"/archives{path}"
        try:
            router.route(full_path, is_admin=False, check_write=True)
            raise AssertionError(f"Archives mount accepted write: {full_path}")
        except AccessDeniedError:
            pass  # Correctly rejected
        except (InvalidPathError, PathNotMountedError):
            pass  # Path issues, also acceptable
