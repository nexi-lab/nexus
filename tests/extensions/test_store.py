"""ManifestStore tests — list/get/check/resolve_factory + lazy invariants."""

from __future__ import annotations

import pytest

from nexus.extensions.errors import DuplicateManifestError
from nexus.extensions.store import CheckReport, ManifestStore

pytest_plugins = ["tests.extensions.fixtures.conftest"]


class TestStoreBasics:
    def test_empty_store_lists_nothing(self):
        store = ManifestStore()
        assert store.list() == []

    def test_register_and_list(self, hn_manifest):
        store = ManifestStore()
        store._register(hn_manifest, source="test")
        assert store.list() == [hn_manifest]

    def test_register_multiple_kinds(self, all_manifests):
        store = ManifestStore()
        for m in all_manifests:
            store._register(m, source="test")
        assert len(store.list()) == 3
        assert len(store.list(kind="connector")) == 1
        assert len(store.list(kind="brick")) == 1
        assert len(store.list(kind="plugin")) == 1

    def test_get_by_name_and_kind(self, hn_manifest):
        store = ManifestStore()
        store._register(hn_manifest, source="test")
        assert store.get("hn", kind="connector") is hn_manifest

    def test_get_unknown_raises_keyerror(self):
        store = ManifestStore()
        with pytest.raises(KeyError):
            store.get("ghost", kind="connector")

    def test_duplicate_same_source_raises(self, hn_manifest):
        store = ManifestStore()
        store._register(hn_manifest, source="entry_point")
        with pytest.raises(DuplicateManifestError) as excinfo:
            store._register(hn_manifest, source="entry_point")
        assert excinfo.value.kind == "connector"
        assert excinfo.value.name == "hn"


class TestCheckReport:
    def test_check_report_shape(self):
        report = CheckReport(
            available=True,
            missing_python_deps=(),
            missing_binary_deps=(),
            missing_services=(),
            import_probe_failures=(),
            profile_gate_disabled=False,
        )
        assert report.available is True


class TestSourcePrecedence:
    def test_first_source_wins(self, hn_manifest):
        """When the same (kind, name) is registered from different sources,
        the first one wins — JSON index > entry-points > fs scan."""
        from nexus.extensions.manifest import ConnectorManifest

        store = ManifestStore()
        store._register(hn_manifest, source="json_index")
        alt = ConnectorManifest(
            name="hn",
            module="some.other.module",
            factory="Other",
            service_name="hn",
        )
        store._register(alt, source="entry_point")  # ignored
        got = store.get("hn", kind="connector")
        assert got.module == hn_manifest.module

    def test_get_returns_winning_source(self, hn_manifest):
        store = ManifestStore()
        store._register(hn_manifest, source="entry_point")
        store._register(hn_manifest, source="fs_scan")  # ignored
        assert store.get("hn", kind="connector") is hn_manifest

    def test_different_kinds_same_name_coexist(self):
        from nexus.extensions.manifest import ConnectorManifest, PluginManifest

        store = ManifestStore()
        c = ConnectorManifest(name="foo", module="m", factory="F", service_name="foo")
        p = PluginManifest(name="foo", module="m", factory="F")
        store._register(c, source="test")
        store._register(p, source="test")
        assert store.get("foo", kind="connector") is c
        assert store.get("foo", kind="plugin") is p


class TestProfileFilter:
    def test_no_profile_filter_returns_all(self, all_manifests):
        store = ManifestStore()
        for m in all_manifests:
            store._register(m, source="test")
        assert len(store.list()) == 3

    def test_profile_filter_includes_ungated(self, all_manifests):
        """Manifests with profile_gate=None always appear; gated ones filtered."""
        store = ManifestStore()
        for m in all_manifests:
            store._register(m, source="test")
        listed = store.list(profile=frozenset({"other"}))
        names = {m.name for m in listed}
        assert "hn" in names
        assert "koi" in names
        assert "search" not in names

    def test_profile_filter_includes_matching_gate(self, all_manifests):
        store = ManifestStore()
        for m in all_manifests:
            store._register(m, source="test")
        listed = store.list(profile=frozenset({"search"}))
        names = {m.name for m in listed}
        assert "search" in names
