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
    # Snapshot + restore so we don't affect other tests in the integration run.
    names_before = set(ConnectorRegistry.list_available())
    yield
    for nm in set(ConnectorRegistry.list_available()) - names_before:
        ConnectorRegistry._base._items.pop(nm, None)


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

    def test_missing_python_dep_raises(self) -> None:
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
