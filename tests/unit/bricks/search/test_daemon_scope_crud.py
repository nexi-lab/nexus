"""Unit tests for the daemon's index-scope CRUD policy layer (Issue #3698).

These cover the 8 edge-case policies enumerated in the review for Issue #6.
The router wrapper at ``/api/v2/search/index-directory`` does nothing but
exception-to-HTTP translation, so the policy behavior is verified here
where the tests don't need FastAPI app scaffolding.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus.bricks.search.index_scope import (
    DirectoryAlreadyRegisteredError,
    DirectoryNotRegisteredError,
    InvalidDirectoryPathError,
    ZoneNotFoundError,
)


class _NoOpResult:
    def fetchall(self) -> list:
        return []

    def first(self) -> Any:
        return None

    @property
    def rowcount(self) -> int:
        return 0


class _RecordingSession:
    """An async session that records SQL and can fake row existence."""

    def __init__(self, zone_exists: bool = True) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self._zone_exists = zone_exists

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(stmt)
        self.statements.append((sql, dict(params or {})))

        class _R:
            def __init__(self, hit: bool) -> None:
                self._hit = hit

            def first(self) -> Any:
                return (1,) if self._hit else None

            def fetchall(self) -> list:
                return []

            @property
            def rowcount(self) -> int:
                return 0

        if "FROM zones" in sql:
            return _R(self._zone_exists)
        return _NoOpResult()

    async def commit(self) -> None:
        return None


def _session_factory(session: _RecordingSession) -> Any:
    def _factory() -> _RecordingSession:
        return session

    return _factory


def _make_daemon(
    *,
    zone_exists: bool = True,
    initial_modes: dict[str, str] | None = None,
    initial_dirs: dict[str, set[str]] | None = None,
) -> tuple[Any, _RecordingSession]:
    """Construct a minimally-wired SearchDaemon for CRUD testing."""
    from nexus.bricks.search.daemon import SearchDaemon

    session = _RecordingSession(zone_exists=zone_exists)
    daemon = SearchDaemon.__new__(SearchDaemon)
    # Use setattr for protected-attribute injection so mypy doesn't
    # need a per-line suppression here.
    daemon._async_session = _session_factory(session)
    daemon._zone_indexing_modes = dict(initial_modes or {})
    daemon._indexed_directories = {z: set(d) for z, d in (initial_dirs or {}).items()}
    daemon._scope_generation = 0
    daemon._refresh_lock = asyncio.Lock()
    daemon._backend = None
    return daemon, session


# =============================================================================
# Policy 1 — Non-existent directory paths are ALLOWED
# =============================================================================


@pytest.mark.asyncio
async def test_policy1_register_nonexistent_directory_is_allowed() -> None:
    """The daemon does not check VFS existence — register-for-future is OK."""
    daemon, _ = _make_daemon(zone_exists=True)
    canonical, backfill = await daemon.add_indexed_directory("zone_a", "/workspace/future")
    assert canonical == "/workspace/future"
    assert backfill.status in ("ok", "no_op")
    assert "/workspace/future" in daemon._indexed_directories["zone_a"]


# =============================================================================
# Policy 3 — Overlapping prefixes coexist
# =============================================================================


@pytest.mark.asyncio
async def test_policy3_overlapping_directories_coexist() -> None:
    """Registering /src then /src/lib should keep both and reject duplicates."""
    daemon, _ = _make_daemon(zone_exists=True)
    await daemon.add_indexed_directory("zone_a", "/src")
    await daemon.add_indexed_directory("zone_a", "/src/lib")
    assert daemon.list_indexed_directories("zone_a") == ["/src", "/src/lib"]

    # Exact duplicate must still fail.
    with pytest.raises(DirectoryAlreadyRegisteredError):
        await daemon.add_indexed_directory("zone_a", "/src")


# =============================================================================
# Policy 4 — Missing zone raises ZoneNotFoundError (→ 404)
# =============================================================================


@pytest.mark.asyncio
async def test_policy4_missing_zone_raises_zone_not_found() -> None:
    daemon, _ = _make_daemon(zone_exists=False)
    with pytest.raises(ZoneNotFoundError):
        await daemon.add_indexed_directory("ghost_zone", "/src")


# =============================================================================
# Policy 5 — Path escape (`..`) raises InvalidDirectoryPathError (→ 400)
# =============================================================================


@pytest.mark.asyncio
async def test_policy5_path_escape_rejected() -> None:
    daemon, _ = _make_daemon(zone_exists=True)
    with pytest.raises(InvalidDirectoryPathError):
        await daemon.add_indexed_directory("zone_a", "/foo/../bar")


@pytest.mark.asyncio
async def test_policy5_relative_path_rejected() -> None:
    daemon, _ = _make_daemon(zone_exists=True)
    with pytest.raises(InvalidDirectoryPathError):
        await daemon.add_indexed_directory("zone_a", "relative/path")


@pytest.mark.asyncio
async def test_policy5_zone_prefixed_path_rejected() -> None:
    """Callers must strip /zone/{id}/ before passing the path in."""
    daemon, _ = _make_daemon(zone_exists=True)
    with pytest.raises(InvalidDirectoryPathError):
        await daemon.add_indexed_directory("zone_a", "/zone/zone_a/src")


@pytest.mark.asyncio
async def test_policy5_dot_segment_rejected() -> None:
    """A `.` segment in the path is rejected — call-site should canonicalize."""
    daemon, _ = _make_daemon(zone_exists=True)
    with pytest.raises(InvalidDirectoryPathError):
        await daemon.add_indexed_directory("zone_a", "/foo/./bar")


@pytest.mark.asyncio
async def test_policy5_trailing_dotdot_rejected() -> None:
    """`..` anywhere in the path is rejected, not just at the start."""
    daemon, _ = _make_daemon(zone_exists=True)
    with pytest.raises(InvalidDirectoryPathError):
        await daemon.add_indexed_directory("zone_a", "/foo/bar/..")


# =============================================================================
# Policy 6 — Unregistering an absent entry raises DirectoryNotRegisteredError
# =============================================================================


@pytest.mark.asyncio
async def test_policy6_unregister_absent_raises() -> None:
    daemon, _ = _make_daemon(zone_exists=True)
    with pytest.raises(DirectoryNotRegisteredError):
        await daemon.remove_indexed_directory("zone_a", "/never/was")


# =============================================================================
# Policy 8 — Dangling registration (VFS deleted) is allowed to persist
# =============================================================================


@pytest.mark.asyncio
async def test_policy8_dangling_directory_persists_in_list() -> None:
    """VFS deletion does not trigger auto-cleanup — list still shows the dir."""
    daemon, _ = _make_daemon(zone_exists=True)
    await daemon.add_indexed_directory("zone_a", "/stale/dir")
    # Simulate the VFS directory being deleted externally — list still shows it.
    assert "/stale/dir" in daemon.list_indexed_directories("zone_a")


# =============================================================================
# Round-trip — register → list → unregister → list empty
# =============================================================================


@pytest.mark.asyncio
async def test_round_trip_register_list_unregister() -> None:
    daemon, _ = _make_daemon(zone_exists=True)
    await daemon.add_indexed_directory("zone_a", "/src")
    assert daemon.list_indexed_directories("zone_a") == ["/src"]

    await daemon.remove_indexed_directory("zone_a", "/src")
    assert daemon.list_indexed_directories("zone_a") == []
    # Empty set should be pruned from the dict so scope lookups don't
    # see stale keys.
    assert "zone_a" not in daemon._indexed_directories


# =============================================================================
# Canonicalization — trailing slash is stripped on register, list returns canonical
# =============================================================================


@pytest.mark.asyncio
async def test_canonicalization_strips_trailing_slash() -> None:
    daemon, _ = _make_daemon(zone_exists=True)
    canonical, _ = await daemon.add_indexed_directory("zone_a", "/src/")
    assert canonical == "/src"
    assert "/src" in daemon._indexed_directories["zone_a"]
    assert "/src/" not in daemon._indexed_directories["zone_a"]


# =============================================================================
# Mode flip — set_zone_indexing_mode toggles modes and persists
# =============================================================================


@pytest.mark.asyncio
async def test_set_zone_indexing_mode_toggle() -> None:
    daemon, _ = _make_daemon(zone_exists=True, initial_modes={"zone_a": "all"})
    await daemon.set_zone_indexing_mode("zone_a", "scoped")
    assert daemon._zone_indexing_modes["zone_a"] == "scoped"

    await daemon.set_zone_indexing_mode("zone_a", "all")
    assert daemon._zone_indexing_modes["zone_a"] == "all"


@pytest.mark.asyncio
async def test_set_zone_indexing_mode_invalid_mode_rejected() -> None:
    daemon, _ = _make_daemon(zone_exists=True)
    with pytest.raises(InvalidDirectoryPathError):
        await daemon.set_zone_indexing_mode("zone_a", "bogus")


@pytest.mark.asyncio
async def test_set_zone_indexing_mode_missing_zone_raises() -> None:
    daemon, _ = _make_daemon(zone_exists=False)
    with pytest.raises(ZoneNotFoundError):
        await daemon.set_zone_indexing_mode("ghost", "scoped")
