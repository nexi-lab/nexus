"""Unit tests for runtime_deps module (Issue #3830, sub-project A)."""

from __future__ import annotations

import pytest

from nexus.backends.base.runtime_deps import (
    BinaryDep,
    PythonDep,
    RuntimeDep,
    ServiceDep,
)
from tests.testkit.profiles import TestProfile, pytest_profile_params


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


@pytest.mark.parametrize(
    "profile",
    pytest_profile_params("slim", "sandbox", "remote"),
)
def test_runtime_dep_profile_matrix_ids_are_usable(profile: TestProfile) -> None:
    """Smoke-test the shared profile matrix in a backend-facing suite."""
    assert profile.config["profile"] in {"slim", "sandbox", "remote"}


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

    def test_dotted_module_uses_package_field_for_hint(self) -> None:
        """When extras-hint is disabled (full install / ambiguous) a dotted
        module name must fall back to the PythonDep.package field — not the
        raw module name — otherwise the hint reads
        ``pip install google.cloud.storage`` which is not a valid pip target.
        """
        with patch(
            "nexus.backends.base.runtime_deps._nexus_fs_extras_available",
            return_value=False,
        ):
            missing = check_runtime_deps(
                (
                    PythonDep(
                        "definitely_not_real.dotted.module",
                        extras=("gcs",),
                        package="some-pypi-name",
                    ),
                )
            )
        assert len(missing) == 1
        _, reason = missing[0]
        assert "pip install some-pypi-name" in reason
        assert "pip install definitely_not_real.dotted.module" not in reason

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

    def test_token_manager_service_probe_includes_sqlalchemy(self) -> None:
        """``token_manager`` keeps a per-module probe so legacy / third-party
        manifests that still declare ``ServiceDep("token_manager")`` resolve
        against the actually-shipped module rather than falling through to
        ``_server_available()``, which would falsely report missing on slim
        even though the auth/oauth bricks are force-included (Issue #3947).

        The probe tuple lists ``sqlalchemy`` alongside the bricks module
        because token_manager imports sqlalchemy at top level: a base slim
        install ships the bricks file but not sqlalchemy, so a
        presence-only probe would mark the service satisfied and the
        consumer would hit a raw ``ModuleNotFoundError`` later.
        """
        from nexus.backends.base.runtime_deps import _SERVICE_MODULES

        modules = _SERVICE_MODULES["token_manager"]
        assert "nexus.bricks.auth.oauth.token_manager" in modules
        assert "sqlalchemy" in modules

    def test_service_dep_unsatisfied_when_partial_modules_missing(self) -> None:
        """A multi-module service must fail when *any* module is missing —
        otherwise consumers see a false-positive availability and crash at
        import time on a real-world dep gap (Issue #3947).
        """
        from unittest.mock import patch

        from nexus.backends.base.runtime_deps import _service_available

        def _fake_find_spec(name: str) -> object | None:
            # Pretend the bricks module is shipped but sqlalchemy is not —
            # this is exactly the base-slim shape.
            if name == "nexus.bricks.auth.oauth.token_manager":
                return object()
            if name == "sqlalchemy":
                return None
            return object()

        with patch(
            "nexus.backends.base.runtime_deps.importlib.util.find_spec",
            side_effect=_fake_find_spec,
        ):
            assert not _service_available("token_manager")

    def test_service_dep_reevaluates_after_install(self) -> None:
        """Retry-after-install must work: a process that hits a missing
        ``sqlalchemy`` once, then has it installed mid-run, must see the
        next ``check_runtime_deps`` call report the service satisfied
        (Issue #3947). A cached negative would freeze the bad answer.
        """
        from unittest.mock import patch

        # Phase 1: sqlalchemy not yet installed.
        def _missing(name: str) -> object | None:
            if name == "sqlalchemy":
                return None
            return object()

        with patch(
            "nexus.backends.base.runtime_deps.importlib.util.find_spec",
            side_effect=_missing,
        ):
            phase1 = check_runtime_deps((ServiceDep("token_manager"),))
        assert phase1, "expected ServiceDep('token_manager') to be missing pre-install"

        # Phase 2: same process, sqlalchemy now resolves.
        def _present(name: str) -> object | None:
            return object()

        with patch(
            "nexus.backends.base.runtime_deps.importlib.util.find_spec",
            side_effect=_present,
        ):
            phase2 = check_runtime_deps((ServiceDep("token_manager"),))
        assert not phase2, f"ServiceDep('token_manager') still missing after install: {phase2}"

    def test_service_dep_rejects_old_sqlalchemy(self) -> None:
        """A SQLAlchemy 1.x install is importable but missing 2.x APIs that
        ``token_manager`` (via ``nexus.storage.models``) needs. The probe
        must report the service missing rather than passing on presence
        alone (Issue #3947).
        """
        from unittest.mock import patch

        with (
            patch(
                "nexus.backends.base.runtime_deps.importlib.util.find_spec",
                side_effect=lambda _name: object(),
            ),
            patch(
                "nexus.backends.base.runtime_deps.importlib.metadata.version",
                lambda dist: "1.4.49" if dist == "sqlalchemy" else "0.0",
            ),
        ):
            missing = check_runtime_deps((ServiceDep("token_manager"),))
        assert len(missing) == 1
        _, reason = missing[0]
        assert "sqlalchemy" in reason
        assert ">= 2.0" in reason
        assert "pip install 'sqlalchemy>=2.0'" in reason

    def test_service_dep_accepts_sqlalchemy_2x_and_newer(self) -> None:
        """Pinning a minimum must not lock out forward-compatible majors.
        Both ``2.0.30`` and ``2.5.0`` should satisfy ``>= 2.0``.
        """
        from unittest.mock import patch

        for installed in ("2.0.30", "2.5.0"):
            with (
                patch(
                    "nexus.backends.base.runtime_deps.importlib.util.find_spec",
                    side_effect=lambda _name: object(),
                ),
                patch(
                    "nexus.backends.base.runtime_deps.importlib.metadata.version",
                    lambda dist, _v=installed: _v if dist == "sqlalchemy" else "0.0",
                ),
            ):
                missing = check_runtime_deps((ServiceDep("token_manager"),))
            assert not missing, f"sqlalchemy {installed} should satisfy: {missing}"

    def test_parse_version_prefix_handles_pre_release(self) -> None:
        """Version-prefix parser must tolerate PEP 440 suffixes (rc, post,
        dev, etc.) without failing the comparison."""
        from nexus.backends.base.runtime_deps import _parse_version_prefix

        assert _parse_version_prefix("2.0.0rc3") == (2, 0, 0)
        assert _parse_version_prefix("2.1.dev0") == (2, 1)
        assert _parse_version_prefix("2.0.30") == (2, 0, 30)
        assert _parse_version_prefix("not-a-version") == ()

    def test_is_prerelease_classifies_pep440_suffixes(self) -> None:
        """PEP 440 prerelease / dev suffixes must be recognized so the
        version probe can reject them when their numeric prefix equals
        the required final release (Issue #3947)."""
        from nexus.backends.base.runtime_deps import _is_prerelease

        assert _is_prerelease("2.0.0rc1")
        assert _is_prerelease("2.0.0a1")
        assert _is_prerelease("2.0.0b1")
        assert _is_prerelease("2.0.0.dev0")
        assert _is_prerelease("2.0.0alpha1")
        assert _is_prerelease("2.0.0beta1")
        # Post-releases and local versions count as final.
        assert not _is_prerelease("2.0.0")
        assert not _is_prerelease("2.0.0.post1")
        assert not _is_prerelease("2.0.0+local")

    def test_service_dep_rejects_prerelease_at_minimum(self) -> None:
        """A prerelease whose numeric prefix matches the minimum sorts
        strictly below the final release — token_manager must reject it
        because it might be missing 2.0 final APIs (Issue #3947).
        """
        from unittest.mock import patch

        for prerelease in ("2.0.0rc1", "2.0.0a1", "2.0.0b1", "2.0.0.dev0"):
            with (
                patch(
                    "nexus.backends.base.runtime_deps.importlib.util.find_spec",
                    side_effect=lambda _name: object(),
                ),
                patch(
                    "nexus.backends.base.runtime_deps.importlib.metadata.version",
                    lambda dist, _v=prerelease: _v if dist == "sqlalchemy" else "0.0",
                ),
            ):
                missing = check_runtime_deps((ServiceDep("token_manager"),))
            assert missing, f"prerelease {prerelease} must not satisfy >= 2.0"
            _, reason = missing[0]
            assert ">= 2.0" in reason

    def test_service_dep_accepts_prerelease_above_minimum(self) -> None:
        """A prerelease whose numeric position is strictly above the
        minimum may legitimately ship the required APIs — the probe must
        accept it. ``2.5.0rc1`` covers next-minor; ``2.0.1rc1`` and
        ``2.0.30rc1`` cover same-minor patch prereleases that PEP 440
        sorts above ``2.0.0`` final (Issue #3947).
        """
        from unittest.mock import patch

        for prerelease in ("2.5.0rc1", "2.0.1rc1", "2.0.30rc1"):
            with (
                patch(
                    "nexus.backends.base.runtime_deps.importlib.util.find_spec",
                    side_effect=lambda _name: object(),
                ),
                patch(
                    "nexus.backends.base.runtime_deps.importlib.metadata.version",
                    lambda dist, _v=prerelease: _v if dist == "sqlalchemy" else "0.0",
                ),
            ):
                missing = check_runtime_deps((ServiceDep("token_manager"),))
            assert not missing, f"prerelease {prerelease} should satisfy >= 2.0: {missing}"

    def test_service_dep_reason_points_at_missing_python_dep(self) -> None:
        """When a service probe fails because of an installable third-party
        module (e.g. ``sqlalchemy``), the reason text must point users at
        ``pip install <pkg>`` rather than the misleading "full nexus
        install" message (Issue #3947).
        """
        from unittest.mock import patch

        def _bricks_present_sqlalchemy_missing(name: str) -> object | None:
            if name == "sqlalchemy":
                return None
            return object()

        with patch(
            "nexus.backends.base.runtime_deps.importlib.util.find_spec",
            side_effect=_bricks_present_sqlalchemy_missing,
        ):
            missing = check_runtime_deps((ServiceDep("token_manager"),))
        assert len(missing) == 1
        _, reason = missing[0]
        assert "sqlalchemy" in reason
        assert "pip install sqlalchemy" in reason
        assert "full nexus install" not in reason

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
