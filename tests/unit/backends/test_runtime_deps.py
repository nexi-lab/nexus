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


from unittest.mock import patch  # noqa: E402

from nexus.backends.base.runtime_deps import check_runtime_deps  # noqa: E402


class TestCheckRuntimeDeps:
    def test_empty_deps_returns_empty(self) -> None:
        assert check_runtime_deps(()) == []

    def test_satisfied_python_dep(self) -> None:
        # 'json' is always present in stdlib.
        assert check_runtime_deps((PythonDep("json"),)) == []

    def test_missing_python_dep_without_extras(self) -> None:
        missing = check_runtime_deps((PythonDep("definitely_not_a_real_module_xyz"),))
        assert len(missing) == 1
        dep, reason = missing[0]
        assert isinstance(dep, PythonDep)
        assert "pip install definitely_not_a_real_module_xyz" in reason

    def test_missing_python_dep_with_extras(self) -> None:
        # Force the hint formatter to act as if running under the slim
        # distribution so we can assert the nexus-fs extras form. Under
        # the full (nexus-ai-fs) or dev checkout the hint drops back to
        # a raw-module install command, covered separately below.
        with patch(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            return_value=True,
        ):
            missing = check_runtime_deps(
                (PythonDep("definitely_not_a_real_module_xyz", extras=("gcs", "gdrive")),)
            )
        assert len(missing) == 1
        _, reason = missing[0]
        assert "pip install nexus-fs[gcs,gdrive]" in reason

    def test_missing_python_dep_with_extras_on_full_install(self) -> None:
        """Under the full (nexus-ai-fs) install the hint must not recommend
        ``pip install nexus-fs[...]`` — that would install a conflicting
        distribution. Fall back to the raw module name instead."""
        with patch(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            return_value=False,
        ):
            missing = check_runtime_deps(
                (PythonDep("definitely_not_a_real_module_xyz", extras=("gcs",)),)
            )
        assert len(missing) == 1
        _, reason = missing[0]
        assert "nexus-fs" not in reason
        assert "pip install definitely_not_a_real_module_xyz" in reason

    def test_satisfied_binary_dep(self) -> None:
        # 'sh' is on PATH on every POSIX system + in CI images.
        assert check_runtime_deps((BinaryDep("sh", "n/a"),)) == []

    def test_missing_binary_dep(self) -> None:
        missing = check_runtime_deps(
            (BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),)
        )
        assert len(missing) == 1
        _, reason = missing[0]
        assert "not on PATH" in reason
        assert "brew install xyz" in reason

    def test_service_dep_satisfied_when_server_available(self) -> None:
        missing = check_runtime_deps((ServiceDep("token_manager"),), server_available=True)
        assert missing == []

    def test_service_dep_missing_when_slim(self) -> None:
        missing = check_runtime_deps((ServiceDep("token_manager"),), server_available=False)
        assert len(missing) == 1
        _, reason = missing[0]
        assert "service 'token_manager'" in reason
        assert "full nexus install" in reason

    def test_aggregates_all_missing(self) -> None:
        deps: tuple[RuntimeDep, ...] = (
            PythonDep("definitely_not_a_real_module_xyz", extras=("gws",)),
            BinaryDep("definitely_not_a_real_binary_xyz", "brew install xyz"),
            ServiceDep("kernel"),
            PythonDep("json"),  # satisfied — should not appear in output
        )
        missing = check_runtime_deps(deps, server_available=False)
        assert len(missing) == 3
        reasons = [r for _, r in missing]
        assert any("definitely_not_a_real_module_xyz" in r for r in reasons)
        assert any("definitely_not_a_real_binary_xyz" in r for r in reasons)
        assert any("service 'kernel'" in r for r in reasons)

    def test_missing_dotted_python_dep_parent_missing(self) -> None:
        """Regression: importlib.util.find_spec("x.y.z") raises
        ModuleNotFoundError when 'x' is absent; check_runtime_deps must
        treat that as "not installed" rather than letting the exception
        escape. Without the guard the user sees an opaque ModuleNotFoundError
        instead of the intended MissingDependencyError."""
        with patch(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            return_value=True,
        ):
            missing = check_runtime_deps(
                (PythonDep("definitely_not_a_real_parent.child.grandchild", extras=("gcs",)),)
            )
        assert len(missing) == 1
        _, reason = missing[0]
        assert "definitely_not_a_real_parent.child.grandchild" in reason
        assert "pip install nexus-fs[gcs]" in reason

    def test_server_available_is_cached(self) -> None:
        from nexus.backends.base.runtime_deps import _server_available

        _server_available.cache_clear()
        with patch("nexus.backends.base.runtime_deps.importlib.util.find_spec") as mock_find:
            mock_find.return_value = object()
            _server_available()
            _server_available()
            assert mock_find.call_count == 1
        _server_available.cache_clear()


from nexus.contracts.exceptions import BackendError, MissingDependencyError  # noqa: E402


class TestMissingDependencyError:
    def test_is_backend_error(self) -> None:
        err = MissingDependencyError(backend="gws_gmail", missing=[])
        assert isinstance(err, BackendError)

    def test_enumerates_all_missing(self) -> None:
        missing = [
            (
                PythonDep("x", extras=("gws",)),
                "python 'x': install with: pip install nexus-fs[gws]",
            ),
            (
                BinaryDep("gws", "brew install gws"),
                "binary 'gws': not on PATH — install with: brew install gws",
            ),
        ]
        err = MissingDependencyError(backend="gws_gmail", missing=missing)
        msg = str(err)
        assert "gws_gmail" in msg
        assert "2 runtime dep" in msg
        assert "python 'x'" in msg
        assert "binary 'gws'" in msg

    def test_missing_attribute_exposed(self) -> None:
        pairs = [(PythonDep("x"), "python 'x': install with: pip install x")]
        err = MissingDependencyError(backend="x", missing=pairs)
        assert err.missing == pairs

    def test_status_code_is_failed_dependency(self) -> None:
        err = MissingDependencyError(backend="x", missing=[])
        assert err.status_code == 424
        assert err.error_type == "Failed Dependency"
        assert err.is_expected is True
