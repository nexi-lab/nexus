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


class TestResolveFactory:
    def test_resolve_imports_target_module(self, monkeypatch, tmp_path):
        """resolve_factory imports the impl module and returns the named attr."""
        import sys

        mod_path = tmp_path / "synthetic_target.py"
        mod_path.write_text("def make(): return 'hi'\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("synthetic_target", None)

        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(name="synthetic", module="synthetic_target", factory="make")
        store._register(m, source="test")

        factory = store.resolve_factory(m)
        assert callable(factory)
        assert factory() == "hi"
        assert "synthetic_target" in sys.modules

    def test_resolve_unknown_module_raises(self):
        from nexus.extensions.errors import FactoryResolutionError
        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(name="ghost", module="nonexistent.module.path", factory="X")
        store._register(m, source="test")
        with pytest.raises(FactoryResolutionError) as excinfo:
            store.resolve_factory(m)
        assert "nonexistent.module.path" in str(excinfo.value)

    def test_resolve_unknown_factory_raises(self, monkeypatch, tmp_path):
        import sys

        mod_path = tmp_path / "has_no_factory.py"
        mod_path.write_text("x = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("has_no_factory", None)

        from nexus.extensions.errors import FactoryResolutionError
        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(name="bad", module="has_no_factory", factory="missing_callable")
        store._register(m, source="test")
        with pytest.raises(FactoryResolutionError) as excinfo:
            store.resolve_factory(m)
        assert "missing_callable" in str(excinfo.value)


class TestLazyInvariant:
    def test_list_does_not_import_impl(self, monkeypatch, tmp_path):
        """list() and get() must not trigger impl module imports."""
        import sys

        # Create an impl module that records when it's imported.
        impl_path = tmp_path / "lazy_impl_target.py"
        impl_path.write_text(
            "import os\n"
            "with open(os.environ['LAZY_PROBE_FILE'], 'a') as f:\n"
            "    f.write('imported\\n')\n"
            "def F():\n"
            "    return 1\n"
        )
        probe_file = tmp_path / "probe.txt"
        monkeypatch.setenv("LAZY_PROBE_FILE", str(probe_file))
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("lazy_impl_target", None)

        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(name="lazy", module="lazy_impl_target", factory="F")
        store._register(m, source="test")

        # list() and get() — must NOT cause the import.
        assert store.list() == [m]
        assert store.get("lazy", kind="plugin") is m
        assert "lazy_impl_target" not in sys.modules
        assert not probe_file.exists()

        # resolve_factory() — must cause the import.
        factory = store.resolve_factory(m)
        assert factory() == 1
        assert "lazy_impl_target" in sys.modules
        assert probe_file.read_text() == "imported\n"
