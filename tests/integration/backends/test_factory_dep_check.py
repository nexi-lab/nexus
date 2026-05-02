"""Integration test: BackendFactory.create() raises MissingDependencyError.

Covers Issue #3830 - typed runtime-dep check at mount time.
"""

from __future__ import annotations

from typing import Any

import pytest
from testkit.assertions import assert_missing_dependency_error
from testkit.backends import FactoryStubBackend

from nexus.backends.base.factory import BackendFactory
from nexus.backends.base.registry import ConnectorRegistry, register_connector
from nexus.backends.base.runtime_deps import BinaryDep, PythonDep
from nexus.contracts.exceptions import MissingDependencyError


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    # Snapshot + restore so we do not affect other tests in the integration run.
    names_before = set(ConnectorRegistry.list_available())
    yield
    for nm in set(ConnectorRegistry.list_available()) - names_before:
        ConnectorRegistry._base._items.pop(nm, None)


class TestFactoryDepCheck:
    def test_satisfied_deps_allow_instantiation(self) -> None:
        @register_connector(
            "stub_ok",
            runtime_deps=(PythonDep("json"),),  # stdlib - always present
        )
        class _OK(FactoryStubBackend):
            pass

        instance = BackendFactory.create("stub_ok", {})
        assert isinstance(instance, _OK)

    def test_missing_python_dep_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force slim-install hint formatting. Otherwise the raw module
        # name is emitted under the monorepo's full distribution.
        monkeypatch.setattr(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            lambda: True,
        )

        @register_connector(
            "stub_missing_py",
            runtime_deps=(PythonDep("definitely_not_a_real_module_xyz", extras=("gcs",)),),
        )
        class _M(FactoryStubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_missing_py", {})

        assert_missing_dependency_error(
            exc_info.value,
            backend="stub_missing_py",
            count=1,
            missing_names=("definitely_not_a_real_module_xyz",),
            install_hints=("pip install nexus-fs[gcs]",),
        )

    def test_missing_binary_dep_raises(self) -> None:
        @register_connector(
            "stub_missing_bin",
            runtime_deps=(BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),),
        )
        class _M(FactoryStubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_missing_bin", {})

        assert_missing_dependency_error(
            exc_info.value,
            backend="stub_missing_bin",
            count=1,
            missing_names=("definitely_not_a_real_binary_xyz",),
            install_hints=("brew install xyz",),
        )

    def test_all_missing_enumerated_together(self) -> None:
        @register_connector(
            "stub_many_missing",
            runtime_deps=(
                PythonDep("definitely_not_a_real_module_xyz", extras=("gws",)),
                BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),
            ),
        )
        class _M(FactoryStubBackend):
            pass

        with pytest.raises(MissingDependencyError) as exc_info:
            BackendFactory.create("stub_many_missing", {})

        assert_missing_dependency_error(
            exc_info.value,
            backend="stub_many_missing",
            count=2,
            missing_names=(
                "definitely_not_a_real_module_xyz",
                "definitely_not_a_real_binary_xyz",
            ),
        )
