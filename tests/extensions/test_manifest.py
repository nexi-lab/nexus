"""Pydantic discriminated-union manifest contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.extensions.manifest import RuntimeDep


class TestRuntimeDep:
    def test_python_dep(self):
        dep = RuntimeDep(kind="python", name="boto3")
        assert dep.kind == "python"
        assert dep.name == "boto3"
        assert dep.extras == ()
        assert dep.install_hint is None

    def test_binary_dep_with_hint(self):
        dep = RuntimeDep(kind="binary", name="git", install_hint="apt install git")
        assert dep.install_hint == "apt install git"

    def test_service_dep(self):
        dep = RuntimeDep(kind="service", name="postgres")
        assert dep.kind == "service"

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValidationError):
            RuntimeDep(kind="cosmic", name="x")

    def test_extras_immutable(self):
        dep = RuntimeDep(kind="python", name="a", extras=("b", "c"))
        assert dep.extras == ("b", "c")

    def test_json_round_trip(self):
        dep = RuntimeDep(kind="python", name="boto3", extras=("s3",))
        data = dep.model_dump()
        again = RuntimeDep.model_validate(data)
        assert again == dep
