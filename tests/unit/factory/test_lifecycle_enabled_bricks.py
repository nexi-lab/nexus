from types import SimpleNamespace
from typing import Any

import pytest

from nexus.factory._lifecycle import _wire_services


class _DummyParsersBrick:
    def __init__(self, parsing_config: Any) -> None:
        self.parsing_config = parsing_config
        self.parser_registry = object()
        self.provider_registry = object()

    def create_parse_fn(self):
        return lambda *_args, **_kwargs: None


class _DummyCacheBrick:
    def __init__(self, cache_store: Any, record_store: Any) -> None:
        self.cache_store = cache_store
        self.record_store = record_store


class _DummyPermissionChecker:
    def __init__(self, **_kwargs: Any) -> None:
        pass


class _DummyDriverCoordinator:
    pass


@pytest.mark.asyncio
async def test_wire_services_passes_enabled_bricks_to_wired_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_boot_post_kernel_services(
        nx: Any,
        router: Any,
        services: dict[str, Any],
        svc_on: Any,
        *,
        security_config: Any = None,
    ):
        captured["services"] = dict(services)
        captured["svc_on"] = svc_on
        captured["security_config"] = security_config
        return {}

    async def _fake_enlist_services(nx: Any, services: dict[str, Any]) -> None:
        return None

    async def _fake_sys_setattr(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("nexus.bricks.parsers.brick.ParsersBrick", _DummyParsersBrick)
    monkeypatch.setattr("nexus.cache.brick.CacheBrick", _DummyCacheBrick)
    monkeypatch.setattr("nexus.bricks.rebac.checker.PermissionChecker", _DummyPermissionChecker)
    monkeypatch.setattr(
        "nexus.factory._wired._boot_post_kernel_services", _fake_boot_post_kernel_services
    )
    monkeypatch.setattr("nexus.factory.service_routing.enlist_services", _fake_enlist_services)

    nx = SimpleNamespace(
        _permission_enforcer=None,
        _parse_config=None,
        cache_store=object(),
        _record_store=object(),
        _cache_config=SimpleNamespace(),
        metadata=object(),
        _init_cred=None,
        _perm_config=SimpleNamespace(enforce=True),
        router=SimpleNamespace(),
        _driver_coordinator=_DummyDriverCoordinator(),
        sys_setattr=_fake_sys_setattr,
        service=lambda _name: None,
    )

    bricks = frozenset({"search", "auth", "pay"})
    await _wire_services(nx, services={}, enabled_bricks=bricks)

    wired_services = captured["services"]
    assert wired_services["enabled_bricks"] == bricks
