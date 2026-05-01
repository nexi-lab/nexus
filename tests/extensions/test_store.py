"""ManifestStore tests — list/get/check/resolve_factory + lazy invariants."""

from __future__ import annotations

import pytest

from nexus.extensions.errors import DuplicateManifestError
from nexus.extensions.store import CheckReport, ManifestStore


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


class TestJsonIndexLoader:
    SCHEMA_VERSION = 1

    def _index_payload(self, manifests):
        return {
            "schema_version": self.SCHEMA_VERSION,
            "generated_at": "2026-04-30T12:00:00Z",
            "manifests": sorted(
                (m.model_dump(mode="json") for m in manifests),
                key=lambda d: (d["kind"], d["name"]),
            ),
        }

    def test_load_index_round_trip(self, tmp_path, all_manifests):
        import json

        index_file = tmp_path / "extensions.json"
        index_file.write_text(json.dumps(self._index_payload(all_manifests)))

        store = ManifestStore()
        store.load_json_index(index_file)

        assert {m.name for m in store.list()} == {"hn", "search", "koi"}

    def test_missing_index_falls_back_silently(self, tmp_path, caplog):
        import logging

        store = ManifestStore()
        with caplog.at_level(logging.INFO):
            store.load_json_index(tmp_path / "does_not_exist.json")

        assert store.list() == []
        assert any("extensions.json" in r.message for r in caplog.records)

    def test_corrupt_json_raises(self, tmp_path):
        from nexus.extensions.errors import IndexCorruptError

        bad = tmp_path / "extensions.json"
        bad.write_text("{ not json")
        store = ManifestStore()
        with pytest.raises(IndexCorruptError):
            store.load_json_index(bad)

    def test_schema_version_mismatch_warns_and_skips(self, tmp_path, caplog):
        import json
        import logging

        bad = tmp_path / "extensions.json"
        bad.write_text(json.dumps({"schema_version": 999, "generated_at": "x", "manifests": []}))

        store = ManifestStore()
        with caplog.at_level(logging.WARNING):
            store.load_json_index(bad)

        assert store.list() == []
        assert any("schema_version" in r.message for r in caplog.records)


class TestEntryPointLoader:
    def test_load_from_entry_points(self, monkeypatch):
        """Entry points whose target is a `_manifest` module are registered."""
        import sys
        import types
        from importlib.metadata import EntryPoint

        fake_mod = types.ModuleType("fake_pkg.alpha._manifest")
        from nexus.extensions.manifest import ConnectorManifest

        fake_mod.MANIFEST = ConnectorManifest(
            name="alpha",
            module="fake_pkg.alpha.connector",
            factory="F",
            service_name="alpha",
        )
        sys.modules["fake_pkg.alpha._manifest"] = fake_mod

        ep = EntryPoint(
            name="alpha",
            value="fake_pkg.alpha._manifest",
            group="nexus.connectors",
        )

        def fake_entry_points(group: str):
            if group == "nexus.connectors":
                return [ep]
            return []

        monkeypatch.setattr("nexus.extensions.store._entry_points", fake_entry_points)

        store = ManifestStore()
        store.load_entry_points()

        names = {m.name for m in store.list()}
        assert "alpha" in names

    def test_entry_point_import_failure_isolated(self, monkeypatch, caplog):
        """A broken entry point logs WARN and doesn't block others."""
        import logging
        import sys
        import types
        from importlib.metadata import EntryPoint

        good_ep = EntryPoint(
            name="good",
            value="fake_pkg.good._manifest",
            group="nexus.plugins",
        )
        bad_ep = EntryPoint(
            name="bad",
            value="nonexistent.module._manifest",
            group="nexus.plugins",
        )

        good_mod = types.ModuleType("fake_pkg.good._manifest")
        from nexus.extensions.manifest import PluginManifest

        good_mod.MANIFEST = PluginManifest(name="good", module="m", factory="F")
        sys.modules["fake_pkg.good._manifest"] = good_mod

        def fake_entry_points(group: str):
            if group == "nexus.plugins":
                return [good_ep, bad_ep]
            return []

        monkeypatch.setattr("nexus.extensions.store._entry_points", fake_entry_points)

        store = ManifestStore()
        with caplog.at_level(logging.WARNING):
            store.load_entry_points()

        assert {m.name for m in store.list()} == {"good"}
        assert any("bad" in r.message for r in caplog.records)


class TestFilesystemLoader:
    def test_load_from_directory(self, tmp_path):
        """Scan a directory tree for `_manifest.py` files and register MANIFEST."""
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "_manifest.py").write_text(
            "from nexus.extensions.manifest import ConnectorManifest\n"
            "MANIFEST = ConnectorManifest(\n"
            "    name='alpha', module='m.alpha', factory='F', service_name='alpha',\n"
            ")\n"
        )
        (tmp_path / "beta").mkdir()
        (tmp_path / "beta" / "_manifest.py").write_text(
            "from nexus.extensions.manifest import BrickManifest\n"
            "MANIFEST = BrickManifest(\n"
            "    name='beta', module='m.beta', factory='F',\n"
            "    tier='independent', result_key='r',\n"
            ")\n"
        )
        # gamma has __init__.py but no _manifest.py — must be ignored.
        (tmp_path / "gamma").mkdir()
        (tmp_path / "gamma" / "__init__.py").write_text("")

        store = ManifestStore()
        store.load_filesystem(tmp_path)

        assert {m.name for m in store.list()} == {"alpha", "beta"}

    def test_filesystem_load_skips_broken_module(self, tmp_path, caplog):
        """A broken `_manifest.py` doesn't block siblings; warning logged."""
        import logging

        (tmp_path / "good").mkdir()
        (tmp_path / "good" / "_manifest.py").write_text(
            "from nexus.extensions.manifest import PluginManifest\n"
            "MANIFEST = PluginManifest(name='good', module='m', factory='F')\n"
        )
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "_manifest.py").write_text("raise RuntimeError('intentional')\n")

        store = ManifestStore()
        with caplog.at_level(logging.WARNING):
            store.load_filesystem(tmp_path)

        assert {m.name for m in store.list()} == {"good"}
        assert any("broken" in r.message for r in caplog.records)


class TestCheckMethod:
    def test_check_all_available(self):
        from nexus.extensions.manifest import PluginManifest, RuntimeDep

        store = ManifestStore()
        m = PluginManifest(
            name="ok",
            module="m",
            factory="F",
            runtime_deps=(RuntimeDep(kind="python", name="sys"),),
            import_probes=("sys",),
        )
        store._register(m, source="test")
        report = store.check(m)
        assert report.available is True
        assert report.missing_python_deps == ()
        assert report.import_probe_failures == ()

    def test_check_missing_python_dep(self):
        from nexus.extensions.manifest import PluginManifest, RuntimeDep

        store = ManifestStore()
        m = PluginManifest(
            name="needs",
            module="m",
            factory="F",
            runtime_deps=(RuntimeDep(kind="python", name="totally_not_a_real_pkg_xyz"),),
            import_probes=("totally_not_a_real_pkg_xyz",),
        )
        store._register(m, source="test")
        report = store.check(m)
        assert report.available is False
        assert "totally_not_a_real_pkg_xyz" in report.import_probe_failures

    def test_check_does_not_import_impl(self, monkeypatch, tmp_path):
        """check() must not import the impl module — only probes."""
        import sys

        impl = tmp_path / "impl_no_import.py"
        impl.write_text("raise RuntimeError('impl import side-effect')\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("impl_no_import", None)

        from nexus.extensions.manifest import PluginManifest

        store = ManifestStore()
        m = PluginManifest(
            name="lazy_check",
            module="impl_no_import",
            factory="F",
            import_probes=("sys",),
        )
        store._register(m, source="test")
        report = store.check(m)
        assert report.available is True
        assert "impl_no_import" not in sys.modules


class TestSingleton:
    def test_get_store_returns_same_instance(self):
        from nexus.extensions.store import get_store, reset_store

        reset_store()
        s1 = get_store()
        s2 = get_store()
        assert s1 is s2

    def test_reset_store_clears_state(self, hn_manifest):
        from nexus.extensions.store import get_store, reset_store

        reset_store()
        s1 = get_store()
        s1._register(hn_manifest, source="test")
        assert any(m.name == "hn" for m in s1.list())

        reset_store()
        s2 = get_store()
        assert s2 is not s1
        assert all(m.name != "hn" for m in s2.list())


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


class _FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint — name + value only."""

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


class TestEntryPointLegacyFormat:
    """Regression: legacy `module:Class` plugin entry points must register
    a synthesized PluginManifest WITHOUT importing the implementation."""

    def test_legacy_class_target_is_registered_without_import(self, monkeypatch, tmp_path):
        import sys

        from nexus.extensions import store as store_mod

        # Build an impl-like module that would explode if imported.
        impl_path = tmp_path / "fake_legacy_plugin.py"
        impl_path.write_text("raise RuntimeError('must not import')\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("fake_legacy_plugin", None)

        ep = _FakeEntryPoint(name="legacy", value="fake_legacy_plugin:LegacyPlugin")
        monkeypatch.setattr(
            store_mod,
            "_entry_points",
            lambda group: [ep] if group == "nexus.plugins" else [],
        )

        store = ManifestStore()
        store.load_entry_points()

        m = store.get("legacy", kind="plugin")
        assert m.module == "fake_legacy_plugin"
        assert m.factory == "LegacyPlugin"
        assert "fake_legacy_plugin" not in sys.modules

    def test_module_style_target_imports_and_reads_manifest(self, monkeypatch, tmp_path):
        import sys

        from nexus.extensions import store as store_mod

        manifest_mod = tmp_path / "fake_module_target.py"
        manifest_mod.write_text(
            "from nexus.extensions.manifest import PluginManifest\n"
            "MANIFEST = PluginManifest(name='modstyle', module='m', factory='F')\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("fake_module_target", None)

        ep = _FakeEntryPoint(name="modstyle", value="fake_module_target")
        monkeypatch.setattr(
            store_mod,
            "_entry_points",
            lambda group: [ep] if group == "nexus.plugins" else [],
        )

        store = ManifestStore()
        store.load_entry_points()
        assert store.get("modstyle", kind="plugin").module == "m"


class TestCheckSemantics:
    """Regression: check() must surface unchecked services and missing binaries."""

    def test_missing_binary_dep_marks_unavailable(self):
        from nexus.extensions.manifest import PluginManifest, RuntimeDep

        m = PluginManifest(
            name="needs_bin",
            module="m",
            factory="F",
            runtime_deps=(
                RuntimeDep(kind="binary", name="this_binary_is_definitely_not_on_path_xyz"),
            ),
        )
        report = ManifestStore().check(m)
        assert report.available is False
        assert "this_binary_is_definitely_not_on_path_xyz" in report.missing_binary_deps

    def test_present_binary_dep_does_not_falsely_block(self):
        from nexus.extensions.manifest import PluginManifest, RuntimeDep

        # `sh` is on PATH on every POSIX system the test suite runs on.
        m = PluginManifest(
            name="needs_sh",
            module="m",
            factory="F",
            runtime_deps=(RuntimeDep(kind="binary", name="sh"),),
        )
        report = ManifestStore().check(m)
        assert report.missing_binary_deps == ()

    def test_service_dep_is_unchecked_and_blocks_available(self):
        """Services can't be probed without a connection — report as unchecked
        and treat the manifest as not-known-available."""
        from nexus.extensions.manifest import PluginManifest, RuntimeDep

        m = PluginManifest(
            name="needs_db",
            module="m",
            factory="F",
            runtime_deps=(RuntimeDep(kind="service", name="postgres"),),
        )
        report = ManifestStore().check(m)
        assert "postgres" in report.missing_services
        assert report.available is False

    def test_python_dep_uses_find_spec_not_string_match(self):
        """Python deps must be checked against the import system, not against
        whether the name happens to appear in the import_probes tuple."""
        from nexus.extensions.manifest import PluginManifest, RuntimeDep

        m = PluginManifest(
            name="needs_pkg",
            module="m",
            factory="F",
            runtime_deps=(RuntimeDep(kind="python", name="totally_made_up_pkg_qwerty"),),
            import_probes=(),  # no probe overlap — must still be flagged
        )
        report = ManifestStore().check(m)
        assert report.available is False
        assert "totally_made_up_pkg_qwerty" in report.missing_python_deps


class TestSingletonThreadSafety:
    """Regression: concurrent first calls to get_store must all see the same
    instance (no torn writes, no lost registrations)."""

    def test_concurrent_first_call_returns_same_instance(self):
        import threading

        from nexus.extensions.store import get_store, reset_store

        reset_store()
        results: list[ManifestStore] = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(get_store())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 8
        first = results[0]
        assert all(r is first for r in results), "torn singleton init"
