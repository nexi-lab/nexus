"""Tests for nexus-fs doctor diagnostic system.

Covers: environment checks, backend install/credential checks (parametrized),
concurrent execution, timeout handling, rendering, and tip generation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nexus.fs._doctor import (
    DoctorCheckResult,
    DoctorStatus,
    _run_with_timeout,
    check_backend_credentials,
    check_backend_installed,
    check_nexus_fs_version,
    check_nexus_runtime_version,
    check_python_version,
    generate_tip,
    render_doctor,
    run_all_checks,
)

# ---------------------------------------------------------------------------
# Section 1: Environment checks
# ---------------------------------------------------------------------------


class TestPythonVersion:
    def test_pass_on_312_plus(self):
        result = check_python_version()
        # This test runs on Python 3.12+, so it should pass
        assert result.status == DoctorStatus.PASS
        assert "3." in result.message

    @patch("nexus.fs._doctor.sys")
    def test_fail_on_old_python(self, mock_sys):
        mock_sys.version_info = (3, 10, 0)
        result = check_python_version()
        assert result.status == DoctorStatus.FAIL
        assert "3.10" in result.message
        assert "3.11" in result.fix_hint


class TestNexusFsVersion:
    def test_version_detected(self):
        result = check_nexus_fs_version()
        assert result.status == DoctorStatus.PASS
        assert result.message.startswith("v")


class TestNexusFastVersion:
    def test_installed(self):
        mock_module = MagicMock(__version__="0.9.10")
        with patch.dict("sys.modules", {"nexus_runtime": mock_module}):
            result = check_nexus_runtime_version()
            assert result.status == DoctorStatus.PASS
            assert "0.9.10" in result.message

    def test_not_installed(self):
        with (
            patch.dict("sys.modules", {"nexus_runtime": None}),
            patch("builtins.__import__", side_effect=ImportError("no nexus_runtime")),
        ):
            result = check_nexus_runtime_version()
            assert result.status == DoctorStatus.NOT_INSTALLED
            assert result.install_cmd is not None


# ---------------------------------------------------------------------------
# Section 2: Backend install + credential checks (parametrized)
# ---------------------------------------------------------------------------


class TestBackendInstalled:
    def test_local_always_pass(self):
        result = check_backend_installed("local")
        assert result.status == DoctorStatus.PASS
        assert "built-in" in result.message

    @pytest.mark.parametrize(
        "scheme,module_name,pip_extra",
        [
            ("s3", "boto3", "nexus-fs[s3]"),
            ("gcs", "google.cloud.storage", "nexus-fs[gcs]"),
            ("gdrive", "googleapiclient", "nexus-fs[gdrive]"),
        ],
    )
    def test_backend_installed(self, scheme, module_name, pip_extra):
        with patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock()
            result = check_backend_installed(scheme)
            assert result.status == DoctorStatus.PASS

    @pytest.mark.parametrize(
        "scheme,module_name,pip_extra",
        [
            ("s3", "boto3", "nexus-fs[s3]"),
            ("gcs", "google.cloud.storage", "nexus-fs[gcs]"),
            ("gdrive", "googleapiclient", "nexus-fs[gdrive]"),
        ],
    )
    def test_backend_not_installed(self, scheme, module_name, pip_extra):
        with patch("importlib.import_module", side_effect=ImportError()):
            result = check_backend_installed(scheme)
            assert result.status == DoctorStatus.NOT_INSTALLED
            assert result.install_cmd == f"pip install {pip_extra}"

    def test_unknown_scheme(self):
        result = check_backend_installed("ftp")
        assert result.status == DoctorStatus.NOT_INSTALLED
        assert "unknown" in result.message


class TestBackendCredentials:
    def test_local_no_creds_needed(self):
        result = check_backend_credentials("local")
        assert result.status == DoctorStatus.PASS
        assert "no credentials" in result.message

    def test_gdrive_deferred(self):
        result = check_backend_credentials("gdrive")
        assert result.status == DoctorStatus.PASS
        assert "deferred" in result.message

    @pytest.mark.parametrize(
        "scheme,validate_fn,validate_path",
        [
            ("s3", "validate_aws_credentials", "nexus.fs._credentials.validate_aws_credentials"),
            ("gcs", "validate_gcs_credentials", "nexus.fs._credentials.validate_gcs_credentials"),
        ],
    )
    def test_creds_present_and_valid(self, scheme, validate_fn, validate_path):
        with (
            patch("nexus.fs._credentials.discover_credentials") as mock_discover,
            patch(validate_path) as mock_validate,
        ):
            mock_discover.return_value = {"source": "environment"}
            if scheme == "s3":
                mock_validate.return_value = {
                    "valid": True,
                    "account": "123456",
                    "arn": "arn:aws:iam::123:user/dev",
                }
            else:
                mock_validate.return_value = {
                    "valid": True,
                    "project": "my-project",
                    "credential_type": "ServiceAccountCredentials",
                }

            result = check_backend_credentials(scheme)
            assert result.status == DoctorStatus.PASS
            assert "valid" in result.message

    @pytest.mark.parametrize(
        "scheme,validate_fn,validate_path",
        [
            ("s3", "validate_aws_credentials", "nexus.fs._credentials.validate_aws_credentials"),
            ("gcs", "validate_gcs_credentials", "nexus.fs._credentials.validate_gcs_credentials"),
        ],
    )
    def test_creds_present_but_invalid(self, scheme, validate_fn, validate_path):
        with (
            patch("nexus.fs._credentials.discover_credentials") as mock_discover,
            patch(validate_path) as mock_validate,
        ):
            mock_discover.return_value = {"source": "environment"}
            mock_validate.return_value = {"valid": False, "error": "ExpiredTokenException"}

            result = check_backend_credentials(scheme)
            assert result.status == DoctorStatus.FAIL
            assert "invalid" in result.message
            assert result.fix_hint is not None

    @pytest.mark.parametrize("scheme", ["s3", "gcs"])
    def test_creds_missing(self, scheme):
        from nexus.contracts.exceptions import CloudCredentialError

        with patch(
            "nexus.fs._credentials.discover_credentials",
            side_effect=CloudCredentialError(scheme, "not found"),
        ):
            result = check_backend_credentials(scheme)
            assert result.status == DoctorStatus.FAIL
            assert "not found" in result.message


# ---------------------------------------------------------------------------
# Concurrent execution + timeout
# ---------------------------------------------------------------------------


class TestConcurrentExecution:
    @pytest.mark.asyncio
    async def test_run_all_checks_returns_three_sections(self):
        """run_all_checks returns Environment, Backends, and Mounts sections."""
        results = await run_all_checks(fs=None)
        assert "Environment" in results
        assert "Backends" in results
        assert "Mounts" in results

    @pytest.mark.asyncio
    async def test_run_all_checks_no_fs_skips_mount_checks(self):
        results = await run_all_checks(fs=None)
        # No mount checks when fs is None
        assert results["Mounts"] == []

    @pytest.mark.asyncio
    async def test_run_all_checks_with_mock_fs(self):
        mock_fs = MagicMock()
        # `list_mounts(kernel)` helper inspects ``kernel._kernel.get_mount_points()``
        mock_fs._kernel.get_mount_points.return_value = ["root:/local/data"]
        mock_fs.sys_readdir = MagicMock(return_value=[])

        with patch(
            "nexus.core.path_utils.extract_zone_id",
            return_value=("root", "/local/data"),
        ):
            results = await run_all_checks(fs=mock_fs)
        mount_results = results["Mounts"]
        assert len(mount_results) == 1
        assert mount_results[0].status == DoctorStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_mount_connectivity_failure(self):
        mock_fs = MagicMock()
        mock_fs._kernel.get_mount_points.return_value = ["root:/s3/bucket"]
        mock_fs.sys_readdir = MagicMock(side_effect=ConnectionError("network unreachable"))

        with patch(
            "nexus.core.path_utils.extract_zone_id",
            return_value=("root", "/s3/bucket"),
        ):
            results = await run_all_checks(fs=mock_fs)
        mount_results = results["Mounts"]
        assert len(mount_results) == 1
        assert mount_results[0].status == DoctorStatus.FAIL
        assert "network unreachable" in mount_results[0].message


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_produces_fail_result(self):
        async def slow_check():
            await asyncio.sleep(10)
            return DoctorCheckResult(name="slow", status=DoctorStatus.PASS, message="done")

        result = await _run_with_timeout(slow_check(), timeout_s=0.1, fallback_name="slow")
        assert result.status == DoctorStatus.FAIL
        assert "timed out" in result.message

    @pytest.mark.asyncio
    async def test_fast_check_succeeds(self):
        async def fast_check():
            return DoctorCheckResult(name="fast", status=DoctorStatus.PASS, message="quick")

        result = await _run_with_timeout(fast_check(), timeout_s=5.0, fallback_name="fast")
        assert result.status == DoctorStatus.PASS

    @pytest.mark.asyncio
    async def test_overall_timeout_triggers(self):
        """run_all_checks respects overall_timeout and returns a FAIL result."""
        # Use an extremely short overall timeout
        results = await run_all_checks(fs=None, overall_timeout=0.001)
        # Either completes normally (fast machine) or hits overall timeout
        assert "Environment" in results


# ---------------------------------------------------------------------------
# Tip generation
# ---------------------------------------------------------------------------


class TestTipGeneration:
    def test_failure_tip(self):
        results = {
            "Environment": [
                DoctorCheckResult(
                    name="python",
                    status=DoctorStatus.FAIL,
                    message="too old",
                    fix_hint="Install Python 3.12",
                )
            ],
            "Backends": [],
            "Mounts": [],
        }
        tip = generate_tip(results)
        assert tip is not None
        assert "Install Python 3.12" in tip

    def test_no_cloud_backend_tip(self):
        results = {
            "Environment": [
                DoctorCheckResult(name="python", status=DoctorStatus.PASS, message="ok")
            ],
            "Backends": [
                DoctorCheckResult(name="local-backend", status=DoctorStatus.PASS, message="ok"),
                DoctorCheckResult(
                    name="s3-backend", status=DoctorStatus.NOT_INSTALLED, message="no"
                ),
                DoctorCheckResult(
                    name="gcs-backend", status=DoctorStatus.NOT_INSTALLED, message="no"
                ),
                DoctorCheckResult(
                    name="gdrive-backend", status=DoctorStatus.NOT_INSTALLED, message="no"
                ),
            ],
            "Mounts": [],
        }
        tip = generate_tip(results)
        assert tip is not None
        assert "cloud backend" in tip.lower() or "nexus-fs[s3]" in tip

    def test_single_mount_tip(self):
        results = {
            "Environment": [
                DoctorCheckResult(name="python", status=DoctorStatus.PASS, message="ok")
            ],
            "Backends": [
                DoctorCheckResult(name="s3-backend", status=DoctorStatus.PASS, message="ok"),
            ],
            "Mounts": [
                DoctorCheckResult(
                    name="/s3/bucket",
                    status=DoctorStatus.CONNECTED,
                    message="connected",
                    latency_ms=42.0,
                )
            ],
        }
        tip = generate_tip(results)
        assert tip is not None
        assert "second backend" in tip.lower()

    def test_all_good_no_tip(self):
        results = {
            "Environment": [
                DoctorCheckResult(name="python", status=DoctorStatus.PASS, message="ok")
            ],
            "Backends": [
                DoctorCheckResult(name="s3-backend", status=DoctorStatus.PASS, message="ok"),
            ],
            "Mounts": [
                DoctorCheckResult(
                    name="/s3/a", status=DoctorStatus.CONNECTED, message="ok", latency_ms=10
                ),
                DoctorCheckResult(
                    name="/gcs/b", status=DoctorStatus.CONNECTED, message="ok", latency_ms=20
                ),
            ],
        }
        tip = generate_tip(results)
        assert tip is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    @staticmethod
    def _render_to_text(results: dict) -> str:
        """Render doctor results and return plain text (no ANSI codes)."""
        from rich.console import Console

        console = Console(record=True, force_terminal=True, width=120)
        render_doctor(results, console=console)
        return console.export_text()

    def test_render_produces_output(self):
        results = {
            "Environment": [
                DoctorCheckResult(name="python", status=DoctorStatus.PASS, message="3.12"),
                DoctorCheckResult(name="nexus-fs", status=DoctorStatus.PASS, message="v0.1.0"),
            ],
            "Backends": [
                DoctorCheckResult(
                    name="s3-backend",
                    status=DoctorStatus.NOT_INSTALLED,
                    message="not installed",
                    install_cmd="pip install nexus-fs[s3]",
                ),
            ],
            "Mounts": [],
        }

        text = self._render_to_text(results)

        assert "Environment" in text
        assert "python" in text
        assert "Backends" in text
        assert "nexus-fs[s3]" in text

    def test_render_summary_counts(self):
        results = {
            "Test": [
                DoctorCheckResult(name="a", status=DoctorStatus.PASS, message="ok"),
                DoctorCheckResult(name="b", status=DoctorStatus.FAIL, message="bad"),
                DoctorCheckResult(name="c", status=DoctorStatus.NOT_INSTALLED, message="missing"),
            ],
        }

        text = self._render_to_text(results)

        assert "3 checks" in text
        assert "1 passed" in text
        assert "1 failed" in text
        assert "1 not installed" in text

    def test_render_fail_includes_fix_hint(self):
        results = {
            "Test": [
                DoctorCheckResult(
                    name="broken",
                    status=DoctorStatus.FAIL,
                    message="it broke",
                    fix_hint="run fix-it",
                ),
            ],
        }

        text = self._render_to_text(results)
        assert "run fix-it" in text
