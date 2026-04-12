"""Regression guard for the bootstrap SQL scope filter (Issue #3698).

The bootstrap query in ``SearchDaemon._bootstrap_txtai_backend`` must push
the scope filter down to SQL when any zone is in ``'scoped'`` mode. A
refactor that accidentally moves the filter into Python would silently
regress scale (the original feature motivation is reducing bootstrap work
for large workspaces).

These tests don't run a real database — they patch the session factory
to intercept the SQL string and assert its shape. Kept intentionally
fragile to the filter clauses so a refactor that drops the filter will
fail loudly.

Issue #3704: bootstrap now uses keyset-paginated ``session.execute()``
(not ``session.stream()``).  The mock returns empty rows on the first
call so the pagination loop terminates immediately; the captured SQL
from that first call is what we verify.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake result — empty rows terminate the keyset pagination loop
# ---------------------------------------------------------------------------


class _FakeResult:
    """Returns no rows so the while-True page loop exits after one call."""

    def fetchall(self) -> list:
        return []


# ---------------------------------------------------------------------------
# Fake session — captures SQL + params via execute(), returns empty result
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        # Record the SQL string and bound params from the first page query.
        self._captured["sql"] = str(stmt)
        self._captured["params"] = params or {}
        return _FakeResult()

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def __call__(self) -> _FakeSession:
        return _FakeSession(self._captured)


class _FakeBackend:
    async def upsert(self, docs: list, *, zone_id: str) -> int:
        return len(docs)


def _make_daemon(
    zone_modes: dict[str, str] | None = None,
    indexed_directories: dict[str, set[str]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Construct a minimally-wired SearchDaemon for SQL inspection."""
    from nexus.bricks.search.daemon import SearchDaemon

    captured: dict[str, Any] = {}
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._async_session = _FakeSessionFactory(captured)
    daemon._backend = _FakeBackend()
    daemon._zone_indexing_modes = zone_modes or {}
    daemon._indexed_directories = indexed_directories or {}
    daemon._txtai_bootstrapped = False

    class _Stats:
        last_index_refresh: float | None = None

    daemon.stats = _Stats()
    return daemon, captured


@pytest.mark.asyncio
async def test_bootstrap_sql_omits_filter_when_all_zones_mode_all() -> None:
    """Fast path: no scoped zones → simple SELECT, no filter clause."""
    daemon, captured = _make_daemon(
        zone_modes={"zone_a": "all", "zone_b": "all"},
    )
    await daemon._bootstrap_txtai_backend()
    sql = captured["sql"]
    assert "FROM document_chunks c" in sql
    assert "JOIN file_paths fp" in sql
    # The scoped filter clauses must be absent on the fast path.
    assert "indexed_directories" not in sql
    assert "scoped_zones" not in sql


@pytest.mark.asyncio
async def test_bootstrap_sql_pushes_filter_when_zone_scoped() -> None:
    """Scoped path: SQL must include the scope filter clauses AND bind
    the list of scoped zone ids."""
    daemon, captured = _make_daemon(
        zone_modes={"zone_a": "scoped", "zone_b": "all"},
        indexed_directories={"zone_a": {"/src"}},
    )
    await daemon._bootstrap_txtai_backend()

    sql = captured["sql"]
    # The filter must push into SQL, not into Python.
    assert "indexed_directories" in sql, (
        "bootstrap SQL must reference indexed_directories (regression: filter "
        "moved to Python). See Issue #3698 review Issue #7."
    )
    assert "scoped_zones" in sql
    # The rule guard: LIKE pattern uses '/%' suffix to block prefix-not-descendant.
    assert "'/%'" in sql or '"/%"' in sql

    # And the bound param must carry the scoped zone ids.
    params = captured["params"]
    assert params.get("scoped_zones") == ["zone_a"]
