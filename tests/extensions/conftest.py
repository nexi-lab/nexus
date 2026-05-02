"""Fixtures for store tests — synthetic manifests covering all kinds."""

from __future__ import annotations

import pytest

from nexus.extensions.manifest import (
    AnyManifest,
    BrickManifest,
    ConnectorManifest,
    PluginManifest,
    RuntimeDep,
)


@pytest.fixture
def hn_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        name="hn",
        module="nexus.backends.connectors.hn.connector",
        factory="HNConnector",
        service_name="hn",
        runtime_deps=(RuntimeDep(kind="python", name="httpx"),),
    )


@pytest.fixture
def search_manifest() -> BrickManifest:
    return BrickManifest(
        name="search",
        module="nexus.bricks.search.brick_factory",
        factory="create",
        tier="independent",
        result_key="search_service",
        profile_gate="search",
    )


@pytest.fixture
def koi_manifest() -> PluginManifest:
    return PluginManifest(
        name="koi",
        module="koi.plugin",
        factory="KoiPlugin",
    )


@pytest.fixture
def all_manifests(
    hn_manifest: ConnectorManifest,
    search_manifest: BrickManifest,
    koi_manifest: PluginManifest,
) -> list[AnyManifest]:
    return [hn_manifest, search_manifest, koi_manifest]
