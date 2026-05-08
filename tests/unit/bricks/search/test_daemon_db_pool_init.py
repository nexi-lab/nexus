"""Regression tests for SearchDaemon engine extraction (Issue #4074).

Before the fix, `_init_database_pool` extracted the SQLAlchemy engine via a
single ``factory.kw["bind"]`` lookup. When that failed silently, the daemon
left ``_async_engine = None`` and reported ``db_pool_ready: false`` forever
while every search request 500-ed with "SearchDaemon not initialized".
"""

from __future__ import annotations

from typing import Any

import pytest


def _daemon_with_factory(factory: Any):
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig()
    daemon._async_session = factory
    daemon._async_engine = None
    daemon._record_store = None
    daemon._owns_engine = False
    return daemon


def test_resolve_via_kw_bind():
    from nexus.bricks.search.daemon import SearchDaemon

    sentinel = object()

    class Factory:
        kw = {"bind": sentinel}

    bind, strategy = SearchDaemon._resolve_injected_engine(Factory())
    assert bind is sentinel
    assert strategy == "kw['bind']"


def test_resolve_via_factory_bind_attr_when_kw_missing_bind():
    """Fallback when factory.kw exists but does not contain 'bind'."""
    from nexus.bricks.search.daemon import SearchDaemon

    sentinel = object()

    class Factory:
        kw: dict = {}  # bind absent
        bind = sentinel

    bind, strategy = SearchDaemon._resolve_injected_engine(Factory())
    assert bind is sentinel
    assert strategy == "factory.bind"


def test_resolve_via_session_bind_when_attrs_missing():
    """Last-resort: construct a session and read its `bind` attr.

    Note we read ``session.bind`` (the original AsyncEngine), not
    ``session.get_bind()`` which proxies to the sync Engine.
    """
    from nexus.bricks.search.daemon import SearchDaemon

    sentinel = object()

    class _Session:
        bind = sentinel

    class Factory:
        # No `kw`, no `bind` attr — must fall back to factory().bind
        def __call__(self):
            return _Session()

    bind, strategy = SearchDaemon._resolve_injected_engine(Factory())
    assert bind is sentinel
    assert strategy == "factory().bind"


def test_resolve_returns_none_when_all_strategies_fail():
    from nexus.bricks.search.daemon import SearchDaemon

    class Factory:
        kw: dict = {}

        def __call__(self):
            raise RuntimeError("cannot construct session")

    bind, strategy = SearchDaemon._resolve_injected_engine(Factory())
    assert bind is None
    assert strategy == "none"


def test_resolve_skips_kw_bind_when_value_is_none():
    """If kw['bind'] is None, must fall through, not return None as success."""
    from nexus.bricks.search.daemon import SearchDaemon

    sentinel = object()

    class Factory:
        kw = {"bind": None}
        bind = sentinel

    bind, strategy = SearchDaemon._resolve_injected_engine(Factory())
    assert bind is sentinel
    assert strategy == "factory.bind"


@pytest.mark.asyncio
async def test_init_database_pool_assigns_engine_from_kw_bind():
    """The canonical async_sessionmaker(bind=engine) path still works."""
    sentinel = object()

    class Factory:
        kw = {"bind": sentinel}

    daemon = _daemon_with_factory(Factory())
    await daemon._init_database_pool()

    assert daemon._async_engine is sentinel


@pytest.mark.asyncio
async def test_init_database_pool_assigns_engine_via_session_fallback():
    """When kw['bind'] is missing, the session fallback succeeds."""
    sentinel = object()

    class _Session:
        bind = sentinel

    class Factory:
        # kw absent entirely (some custom factories don't expose it)
        def __call__(self):
            return _Session()

    daemon = _daemon_with_factory(Factory())
    await daemon._init_database_pool()

    assert daemon._async_engine is sentinel


@pytest.mark.asyncio
async def test_init_database_pool_with_real_async_sessionmaker():
    """End-to-end with a real SQLAlchemy async_sessionmaker.

    Locks the canonical path — the daemon must extract the AsyncEngine
    (not the proxied sync Engine) so downstream ``await engine.connect()``
    calls work.
    """
    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    daemon = _daemon_with_factory(factory)
    await daemon._init_database_pool()

    assert daemon._async_engine is engine
    assert isinstance(daemon._async_engine, AsyncEngine)

    await engine.dispose()


@pytest.mark.asyncio
async def test_init_database_pool_raises_when_engine_unresolvable():
    """Loud failure when no extraction strategy works (Issue #4074)."""

    class Factory:
        kw: dict = {}

        def __call__(self):
            raise RuntimeError("no session for you")

    daemon = _daemon_with_factory(Factory())

    with pytest.raises(RuntimeError, match="engine could not be resolved"):
        await daemon._init_database_pool()

    assert daemon._async_engine is None
