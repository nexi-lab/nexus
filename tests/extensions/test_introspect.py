"""Tests for the public introspection API."""

from __future__ import annotations

import pytest

from nexus.extensions.introspect import (
    check_extension,
    get_extension,
    list_extensions,
    list_kinds,
)
from nexus.extensions.store import reset_store


@pytest.fixture(autouse=True)
def _fresh_store():
    """Reset the singleton before each test so we get a clean slate."""
    reset_store()
    yield
    reset_store()


def _seed(monkeypatch, manifests):
    """Inject manifests into a fresh singleton via monkeypatched bootstrap."""
    from nexus.extensions import store as store_mod

    fake = store_mod.ManifestStore()
    for m in manifests:
        fake._register(m, source="test")
    monkeypatch.setattr(store_mod, "_STORE", fake)


def test_list_extensions_no_filter(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    listed = list_extensions()
    assert {m.name for m in listed} == {"hn", "search", "koi"}


def test_list_extensions_kind_filter(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    listed = list_extensions(kind="connector")
    assert [m.name for m in listed] == ["hn"]


def test_list_extensions_profile_filter(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    listed = list_extensions(profile=frozenset({"search"}))
    names = {m.name for m in listed}
    assert "search" in names  # gate matches
    assert "hn" in names  # ungated → always included
    assert "koi" in names  # ungated → always included


def test_list_extensions_available_only(monkeypatch, all_manifests):
    """Manifest with failing import_probe is filtered out."""
    from nexus.extensions.manifest import PluginManifest

    _seed(
        monkeypatch,
        [
            PluginManifest(name="ok", module="m", factory="F", import_probes=("sys",)),
            PluginManifest(
                name="missing",
                module="m",
                factory="F",
                import_probes=("nonexistent_mod_xyz",),
            ),
        ],
    )
    listed = list_extensions(available_only=True)
    names = {m.name for m in listed}
    assert "ok" in names
    assert "missing" not in names


def test_get_extension(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    m = get_extension("hn", kind="connector")
    assert m.name == "hn"


def test_get_unknown_raises(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    with pytest.raises(KeyError):
        get_extension("ghost", kind="connector")


def test_check_extension(monkeypatch):
    from nexus.extensions.manifest import PluginManifest

    _seed(
        monkeypatch,
        [PluginManifest(name="ok", module="m", factory="F", import_probes=("sys",))],
    )
    report = check_extension("ok", kind="plugin")
    assert report.available is True
    assert report.import_probe_failures == ()


def test_list_kinds():
    kinds = list_kinds()
    assert set(kinds) == {"connector", "brick", "plugin"}
