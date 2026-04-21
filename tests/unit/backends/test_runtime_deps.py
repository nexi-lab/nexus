"""Unit tests for runtime_deps module (Issue #3830, sub-project A)."""

from __future__ import annotations

import pytest

from nexus.backends.base.runtime_deps import (
    BinaryDep,
    PythonDep,
    RuntimeDep,
    ServiceDep,
)


class TestDepTypes:
    def test_python_dep_defaults(self) -> None:
        dep = PythonDep("google.cloud.storage")
        assert dep.module == "google.cloud.storage"
        assert dep.extras == ()

    def test_python_dep_with_extras(self) -> None:
        dep = PythonDep("google.cloud.storage", extras=("gcs",))
        assert dep.extras == ("gcs",)

    def test_python_dep_is_frozen(self) -> None:
        dep = PythonDep("boto3")
        with pytest.raises(AttributeError):
            dep.module = "other"

    def test_binary_dep_requires_hint(self) -> None:
        dep = BinaryDep(name="gws", install_hint="brew install nexi-lab/tap/gws")
        assert dep.name == "gws"
        assert dep.install_hint == "brew install nexi-lab/tap/gws"

    def test_service_dep_name(self) -> None:
        dep = ServiceDep(name="token_manager")
        assert dep.name == "token_manager"

    def test_runtime_dep_union_accepts_all_three(self) -> None:
        deps: tuple[RuntimeDep, ...] = (
            PythonDep("httpx"),
            BinaryDep("gws", "brew install gws"),
            ServiceDep("kernel"),
        )
        assert len(deps) == 3
