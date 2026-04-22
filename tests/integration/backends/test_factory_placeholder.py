"""Factory behavior for placeholder ConnectorInfo entries (Issue #3830 A.3)."""

from __future__ import annotations

import pytest

from nexus.backends._manifest import ConnectorManifestEntry
from nexus.backends.base.factory import BackendFactory
from nexus.backends.base.registry import ConnectorRegistry
from nexus.backends.base.runtime_deps import BinaryDep, PythonDep
from nexus.contracts.exceptions import MissingDependencyError


@pytest.fixture(autouse=True)
def _clean_registry():
    names_before = set(ConnectorRegistry.list_available())
    yield
    for nm in set(ConnectorRegistry.list_available()) - names_before:
        ConnectorRegistry._base.unregister(nm)


class TestFactoryPlaceholder:
    def test_placeholder_with_missing_python_dep_raises_missing_dep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            lambda: True,
        )
        entry = ConnectorManifestEntry(
            name="ph_py_missing",
            module_path="nowhere.real",
            class_name="X",
            description="Placeholder",
            category="storage",
            runtime_deps=(PythonDep("definitely_not_a_real_module_xyz", extras=("gcs",)),),
        )
        ConnectorRegistry.register_placeholder(entry)

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("ph_py_missing", {})
        assert "pip install nexus-fs[gcs]" in str(exc_info.value)

    def test_placeholder_with_missing_binary_raises_missing_dep(self) -> None:
        entry = ConnectorManifestEntry(
            name="ph_bin_missing",
            module_path="nowhere.real",
            class_name="X",
            description="Placeholder",
            category="cli",
            runtime_deps=(BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),),
        )
        ConnectorRegistry.register_placeholder(entry)

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("ph_bin_missing", {})
        assert "brew install xyz" in str(exc_info.value)

    def test_placeholder_with_satisfied_deps_raises_module_failed(self) -> None:
        """Deps met but connector_class still None means import failed for another
        reason (syntax error, etc.) — surface a clear RuntimeError."""
        entry = ConnectorManifestEntry(
            name="ph_deps_ok_import_bad",
            module_path="nowhere.real",
            class_name="X",
            description="Placeholder",
            category="storage",
            runtime_deps=(PythonDep("json"),),  # always present
        )
        ConnectorRegistry.register_placeholder(entry)

        with pytest.raises(RuntimeError) as exc_info:
            BackendFactory.create("ph_deps_ok_import_bad", {})
        msg = str(exc_info.value)
        assert "ph_deps_ok_import_bad" in msg
        assert "failed to import" in msg
