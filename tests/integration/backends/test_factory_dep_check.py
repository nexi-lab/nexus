"""Integration test: BackendFactory.create() raises MissingDependencyError.

Covers Issue #3830 — typed runtime-dep check at mount time.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.backends.base.factory import BackendFactory
from nexus.backends.base.registry import ConnectorRegistry, register_connector
from nexus.backends.base.runtime_deps import BinaryDep, PythonDep
from nexus.contracts.exceptions import MissingDependencyError


class _StubBackend:
    """Minimal ConnectorProtocol-compliant stub."""

    name = "stub"
    write_content = read_content = delete_content = content_exists = None
    get_content_size = mkdir = rmdir = is_directory = None
    user_scoped = False
    is_connected = True
    has_root_path = False
    has_token_manager = False
    backend_features: frozenset = frozenset()

    def check_connection(self) -> None:
        return None

    def has_feature(self, f: Any) -> bool:
        return False

    def __init__(self, **kwargs: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    # Snapshot + restore so real connector registration does not leak across
    # tests. BackendFactory.create("path_s3", config) triggers optional backend
    # registration, so restore the registry contents, one-shot flag, and module
    # imports that control whether connector decorators rerun.
    import sys

    import nexus.backends as backends_mod
    from nexus.backends._manifest import CONNECTOR_MANIFEST

    items_before = dict(ConnectorRegistry._base._items)
    registered_before = backends_mod._optional_backends_registered
    modules_before = {
        entry.module_path: sys.modules.get(entry.module_path) for entry in CONNECTOR_MANIFEST
    }
    yield
    ConnectorRegistry._base._items.clear()
    ConnectorRegistry._base._items.update(items_before)
    backends_mod._optional_backends_registered = registered_before
    for module_path, module in modules_before.items():
        if module is None:
            sys.modules.pop(module_path, None)
        else:
            sys.modules[module_path] = module


class TestFactoryDepCheck:
    def test_satisfied_deps_allow_instantiation(self) -> None:
        @register_connector(
            "stub_ok",
            runtime_deps=(PythonDep("json"),),  # stdlib — always present
        )
        class _OK(_StubBackend):
            pass

        instance = BackendFactory.create("stub_ok", {})
        assert isinstance(instance, _OK)

    def test_missing_python_dep_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force slim-install hint formatting — otherwise the raw module
        # name is emitted under the monorepo's full distribution.
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            lambda: True,
        )

        @register_connector(
            "stub_missing_py",
            runtime_deps=(PythonDep("definitely_not_a_real_module_xyz", extras=("gcs",)),),
        )
        class _M(_StubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_missing_py", {})
        err = exc_info.value
        assert err.backend == "stub_missing_py"
        assert len(err.missing) == 1
        assert "pip install nexus-fs[gcs]" in str(err)

    def test_missing_binary_dep_raises(self) -> None:
        @register_connector(
            "stub_missing_bin",
            runtime_deps=(BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),),
        )
        class _M(_StubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_missing_bin", {})
        assert "brew install xyz" in str(exc_info.value)

    def test_all_missing_enumerated_together(self) -> None:
        @register_connector(
            "stub_many_missing",
            runtime_deps=(
                PythonDep("definitely_not_a_real_module_xyz", extras=("gws",)),
                BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),
            ),
        )
        class _M(_StubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_many_missing", {})
        err = exc_info.value
        assert len(err.missing) == 2
        msg = str(err)
        assert "definitely_not_a_real_module_xyz" in msg
        assert "definitely_not_a_real_binary_xyz" in msg

    def test_path_s3_missing_boto3_uses_s3_extra_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "boto3":
                return None
            return real_find_spec(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            lambda: True,
        )

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("path_s3", {"bucket": "example"})

        msg = str(exc_info.value)
        assert "path_s3" in msg
        assert "boto3" in msg
        assert "pip install nexus-fs[s3]" in msg

    def test_slack_missing_sdk_and_token_manager_are_enumerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "slack_sdk":
                return None
            return real_find_spec(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            lambda: True,
        )
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._service_available",
            lambda name: name != "token_manager",
        )

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("slack_connector", {"token_manager_db": "tokens.db"})

        msg = str(exc_info.value)
        assert "slack_connector" in msg
        assert "slack_sdk" in msg
        assert "pip install nexus-fs[slack]" in msg
        assert "service 'token_manager'" in msg
