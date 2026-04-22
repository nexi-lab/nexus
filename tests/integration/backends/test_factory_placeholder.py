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
        # Failure message must tell users how to recover (restart hint).
        assert "restart" in msg.lower()

    def test_placeholder_surfaces_captured_import_error(self) -> None:
        """Phase-2 import errors captured by record_import_failure must
        appear in the RuntimeError so operators see the real root cause
        without having to hunt through debug logs."""
        from nexus.backends.base.registry import ConnectorRegistry

        entry = ConnectorManifestEntry(
            name="ph_with_captured_err",
            module_path="nowhere.real",
            class_name="X",
            description="Placeholder",
            category="storage",
            runtime_deps=(PythonDep("json"),),
        )
        ConnectorRegistry.register_placeholder(entry)
        ConnectorRegistry.record_import_failure(
            "ph_with_captured_err",
            "SyntaxError: invalid syntax on line 42",
        )

        with pytest.raises(RuntimeError) as exc_info:
            BackendFactory.create("ph_with_captured_err", {})
        msg = str(exc_info.value)
        assert "SyntaxError" in msg
        assert "invalid syntax on line 42" in msg

    def test_unbound_placeholder_rebinds_on_targeted_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After deps are installed, the factory must retry the manifest
        import so the user does not need to restart the process.

        Simulate the flow with a fake module whose ``importlib.import_module``
        side-effect registers the real class. The first ``create()`` call
        should trigger the rebind and succeed.
        """
        import importlib
        import sys
        import types

        from nexus.backends.base.registry import (
            ConnectorInfo,
            register_connector,
        )

        fake_module_name = "nexus_test_rebind_fake_module"

        class RebindBackend:
            """Minimal ConnectorProtocol-compliant stub (not subclassing
            Backend to avoid abstract-method requirements)."""

            CONNECTION_ARGS: dict = {}
            name = "rebind_stub"
            write_content = read_content = delete_content = content_exists = None
            get_content_size = mkdir = rmdir = is_directory = None
            user_scoped = False
            is_connected = True
            has_root_path = False
            has_token_manager = False
            backend_features: frozenset = frozenset()

            def __init__(self, **_: object) -> None:
                pass

            def check_connection(self) -> None:
                return None

            def has_feature(self, _: object) -> bool:
                return False

        RebindBackend.__module__ = fake_module_name
        RebindBackend.__name__ = "RebindBackend"

        fake_module = types.ModuleType(fake_module_name)

        def _install_fake() -> None:
            if fake_module_name in sys.modules:
                return
            # Run the decorator on import, mirroring the real pattern.
            register_connector("ph_rebind_fixture")(RebindBackend)
            sys.modules[fake_module_name] = fake_module

        original_import_module = importlib.import_module

        def _patched_import(name: str, *args, **kwargs):
            if name == fake_module_name:
                _install_fake()
                return sys.modules[name]
            return original_import_module(name, *args, **kwargs)

        monkeypatch.setattr(importlib, "import_module", _patched_import)

        # Pre-register an unbound placeholder that points at our fake
        # module; normally _register_optional_backends handles this, but
        # here we simulate the "import failed first time, dep is now
        # installed" state directly.
        placeholder = ConnectorInfo(
            name="ph_rebind_fixture",
            connector_class=None,
            description="rebind test",
            category="storage",
            runtime_deps=(PythonDep("json"),),
            expected_module_path=fake_module_name,
            expected_class_name="RebindBackend",
        )
        ConnectorRegistry._base.register("ph_rebind_fixture", placeholder, allow_overwrite=True)

        try:
            instance = BackendFactory.create("ph_rebind_fixture", {})
            assert isinstance(instance, RebindBackend)
        finally:
            sys.modules.pop(fake_module_name, None)
