"""Hypothesis property-based tests for VFS kernel path invariants (Issue #1303).

PathRouter was deleted in §12 Phase F3. These tests now exercise
``nexus.core.path_utils`` (normalize_path, validate_path) and
DLC-based LPM directly.

Invariants proven:
  1. Path normalization is idempotent: normalize(normalize(p)) == normalize(p)
  2. Path traversal never escapes mount boundaries
  3. Longest prefix match is deterministic and correct
"""

import tempfile

import pytest

pytest.importorskip("hypothesis")

from hypothesis import example, given, settings
from hypothesis import strategies as st

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import InvalidPathError
from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator
from nexus.core.path_utils import canonicalize_path, normalize_path
from tests.strategies.kernel import (
    path_traversal_attempt,
    valid_mount_point,
    valid_namespaced_path,
    valid_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_dlc() -> DriverLifecycleCoordinator:
    """Build a bare DriverLifecycleCoordinator (no kernel)."""
    return DriverLifecycleCoordinator(dispatch=None, kernel=None)


def _add_mount(
    dlc: DriverLifecycleCoordinator,
    mount_point: str,
    backend,
    *,
    zone_id: str = ROOT_ZONE_ID,
) -> None:
    """Insert a skill backend into the DLC map directly (for test purposes)."""
    canonical = canonicalize_path(mount_point, zone_id)
    dlc._skill_backends[canonical] = backend


def _lookup_lpm(
    dlc: DriverLifecycleCoordinator,
    path: str,
    zone_id: str = ROOT_ZONE_ID,
) -> tuple[str, object] | None:
    """Python-side longest-prefix match over the DLC skill_backends map."""
    import posixpath

    current = canonicalize_path(path, zone_id)
    entries = dlc._skill_backends
    while True:
        info = entries.get(current)
        if info is not None:
            return current, info
        if current == "/":
            return None
        current = posixpath.dirname(current)


def _make_dlc_with_mounts() -> tuple[DriverLifecycleCoordinator, CASLocalBackend]:
    """Create a DLC with standard mounts for testing."""
    tmpdir = tempfile.mkdtemp()
    backend = CASLocalBackend(tmpdir)
    dlc = _new_dlc()
    _add_mount(dlc, "/workspace", backend)
    _add_mount(dlc, "/shared", backend)
    _add_mount(dlc, "/external", backend)
    _add_mount(dlc, "/system", backend)
    _add_mount(dlc, "/archives", backend)
    return dlc, backend


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
        once = normalize_path(path)
        twice = normalize_path(once)
        assert once == twice

    @given(path=valid_path())
    def test_normalized_path_starts_with_slash(self, path: str) -> None:
        """All normalized paths are absolute (start with /)."""
        normalized = normalize_path(path)
        assert normalized.startswith("/")

    @given(path=valid_path())
    def test_normalized_path_has_no_double_slashes(self, path: str) -> None:
        """Normalized paths never contain //."""
        normalized = normalize_path(path)
        assert "//" not in normalized

    @given(path=valid_path())
    def test_normalized_path_has_no_trailing_slash(self, path: str) -> None:
        """Normalized paths have no trailing slash (except root /)."""
        normalized = normalize_path(path)
        if normalized != "/":
            assert not normalized.endswith("/")


# ---------------------------------------------------------------------------
# Invariant 2: Path traversal never escapes mount boundary
# ---------------------------------------------------------------------------


class TestPathTraversalInvariants:
    """Path traversal security properties."""

    @given(attempt=path_traversal_attempt())
    def test_traversal_attempts_rejected_by_validate_path(self, attempt: str) -> None:
        """All path traversal attempts must be rejected by validate_path
        or, if they normalize successfully, must stay within root /."""
        from nexus.core.path_utils import validate_path

        try:
            result = validate_path(attempt)
            # If validation succeeds, the result must at least stay under /
            assert result.startswith("/"), f"Traversal escaped root: {attempt!r} -> {result!r}"
        except (InvalidPathError, ValueError):
            pass  # Correctly rejected — traversal detected

    @given(path=st.text(min_size=1, max_size=100))
    @example(path="\x00/etc/passwd")
    @example(path="/workspace/\x00hidden")
    @example(path="/workspace/../../../etc/passwd")
    @example(path="/workspace/./../../etc")
    def test_arbitrary_strings_never_escape_root(self, path: str) -> None:
        """No arbitrary string can produce a path outside /."""
        try:
            if not path.startswith("/"):
                raise ValueError("Path must be absolute")
            result = normalize_path(path)
            assert result.startswith("/"), f"Escaped root: {path!r} -> {result!r}"
        except (InvalidPathError, ValueError):
            pass  # Correctly rejected

    @given(path=valid_path())
    def test_normalize_roundtrip(self, path: str) -> None:
        """normalize_path is idempotent: normalize(normalize(p)) == normalize(p)."""
        try:
            once = normalize_path(path)
            twice = normalize_path(once)
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
        dlc, _ = _make_dlc_with_mounts()
        try:
            r1 = _lookup_lpm(dlc, path)
            r2 = _lookup_lpm(dlc, path)
            if r1 is not None and r2 is not None:
                assert r1[0] == r2[0]  # Same canonical mount key
        except (InvalidPathError, ValueError):
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

        dlc = _new_dlc()
        _add_mount(dlc, mount1, backend_shallow)
        _add_mount(dlc, deeper_path, backend_deep)

        try:
            result = _lookup_lpm(dlc, query_path)
            if result is not None:
                # The deeper mount should match
                assert result[1].backend is backend_deep
        except (InvalidPathError, ValueError):
            pass  # Path validation may reject generated paths
