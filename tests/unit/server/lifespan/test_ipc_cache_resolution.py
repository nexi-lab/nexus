"""Tests for IPC cache resolution during lifespan startup."""

from types import SimpleNamespace

from nexus.cache import InMemoryCacheStore
from nexus.contracts.cache_store import NullCacheStore
from nexus.server.lifespan.ipc import _resolve_ipc_cache_store
from nexus.server.lifespan.services_container import LifespanServices


def test_prefers_cache_brick_store_over_null_kernel_cache() -> None:
    real_store = InMemoryCacheStore()
    app = SimpleNamespace(
        state=SimpleNamespace(
            cache_brick=SimpleNamespace(
                has_cache_store=True,
                cache_store=real_store,
            )
        )
    )
    svc = LifespanServices(
        nexus_fs=SimpleNamespace(
            cache_store=NullCacheStore(),
            service=lambda _name: None,
        )
    )

    resolved = _resolve_ipc_cache_store(app, svc)

    assert resolved is real_store


def test_falls_back_to_kernel_cache_store_without_cache_brick() -> None:
    kernel_store = InMemoryCacheStore()
    app = SimpleNamespace(state=SimpleNamespace(cache_brick=None))
    svc = LifespanServices(
        nexus_fs=SimpleNamespace(
            cache_store=kernel_store,
            service=lambda _name: None,
        )
    )

    resolved = _resolve_ipc_cache_store(app, svc)

    assert resolved is kernel_store
