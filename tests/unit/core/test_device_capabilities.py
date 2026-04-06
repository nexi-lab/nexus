"""Tests for device capability detection and profile suggestion.

Issue #1708: DeviceCapabilities auto-detection + profile suggestion.

Tests cover:
- DeviceCapabilities dataclass (frozen, defaults)
- get_system_memory_mb() detection with psutil + platform fallbacks
- get_available_memory_mb() detection
- get_cpu_cores() detection + fallback
- has_gpu() detection with env override, shutil.which, subprocess
- detect_capabilities() integration + timing log
- BrickRequirement dataclass
- BRICK_REQUIREMENTS completeness
- bricks_for_device() — table-driven parametrized
- suggest_profile() — table-driven parametrized
- warn_if_profile_exceeds_device() — warning logged
- Smoke test — unmocked detect_capabilities()
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile
from nexus.lib.device_capabilities import (
    BRICK_REQUIREMENTS,
    BrickRequirement,
    DeviceCapabilities,
    bricks_for_device,
    detect_capabilities,
    get_available_memory_mb,
    get_cpu_cores,
    get_system_memory_mb,
    has_gpu,
    suggest_profile,
    warn_if_profile_exceeds_device,
)

# ---------------------------------------------------------------------------
# DeviceCapabilities dataclass
# ---------------------------------------------------------------------------


class TestDeviceCapabilities:
    """Tests for the DeviceCapabilities frozen dataclass."""

    def test_frozen(self) -> None:
        caps = DeviceCapabilities(memory_mb=2048, cpu_cores=4)
        with pytest.raises(AttributeError):
            caps.memory_mb = 4096  # type: ignore[misc]

    def test_defaults(self) -> None:
        caps = DeviceCapabilities(memory_mb=1024)
        assert caps.cpu_cores == 1
        assert caps.has_gpu is False
        assert caps.has_network is True
        assert caps.has_persistent_storage is True

    def test_all_fields(self) -> None:
        caps = DeviceCapabilities(
            memory_mb=8192,
            cpu_cores=8,
            has_gpu=True,
            has_network=False,
            has_persistent_storage=False,
        )
        assert caps.memory_mb == 8192
        assert caps.cpu_cores == 8
        assert caps.has_gpu is True
        assert caps.has_network is False
        assert caps.has_persistent_storage is False


# ---------------------------------------------------------------------------
# get_system_memory_mb()
# ---------------------------------------------------------------------------


class TestGetSystemMemoryMb:
    """Tests for get_system_memory_mb() detection."""

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_MEMORY_MB", "16384")
        assert get_system_memory_mb() == 16384

    def test_psutil_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_MEMORY_MB", raising=False)
        mock_psutil = MagicMock()
        mock_psutil.virtual_memory.return_value.total = 8 * 1024 * 1024 * 1024  # 8 GB
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            result = get_system_memory_mb()
            assert result == 8192

    def test_psutil_missing_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_MEMORY_MB", raising=False)

        def _import_fail(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "psutil":
                raise ImportError("no psutil")
            return original_import(name, *args, **kwargs)

        import builtins

        original_import = builtins.__import__

        with (
            patch("nexus.lib.device_capabilities.platform.system", return_value="Darwin"),
            patch("builtins.__import__", side_effect=_import_fail),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = str(4 * 1024 * 1024 * 1024)  # 4 GB in bytes
            result = get_system_memory_mb()
            assert result == 4096

    def test_fallback_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_MEMORY_MB", raising=False)

        def _import_fail(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "psutil":
                raise ImportError("no psutil")
            return original_import(name, *args, **kwargs)

        import builtins

        original_import = builtins.__import__

        with (
            patch("builtins.__import__", side_effect=_import_fail),
            patch(
                "nexus.lib.device_capabilities.platform.system",
                side_effect=OSError("unknown platform"),
            ),
        ):
            result = get_system_memory_mb()
            assert result == 4096


# ---------------------------------------------------------------------------
# get_available_memory_mb()
# ---------------------------------------------------------------------------


class TestGetAvailableMemoryMb:
    """Tests for get_available_memory_mb() detection."""

    def test_psutil_available(self) -> None:
        mock_psutil = MagicMock()
        mock_psutil.virtual_memory.return_value.available = 4 * 1024 * 1024 * 1024
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            result = get_available_memory_mb()
            assert result == 4096

    def test_fallback(self) -> None:
        def _import_fail(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "psutil":
                raise ImportError("no psutil")
            return original_import(name, *args, **kwargs)

        import builtins

        original_import = builtins.__import__

        with patch("builtins.__import__", side_effect=_import_fail):
            result = get_available_memory_mb()
            assert result == 2048


# ---------------------------------------------------------------------------
# get_cpu_cores()
# ---------------------------------------------------------------------------


class TestGetCpuCores:
    """Tests for get_cpu_cores() detection."""

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_CPU_CORES", "16")
        assert get_cpu_cores() == 16

    def test_os_cpu_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_CPU_CORES", raising=False)
        with patch("os.cpu_count", return_value=8):
            assert get_cpu_cores() == 8

    def test_cpu_count_none_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_CPU_CORES", raising=False)
        with patch("os.cpu_count", return_value=None):
            assert get_cpu_cores() == 1


# ---------------------------------------------------------------------------
# has_gpu()
# ---------------------------------------------------------------------------


class TestHasGpu:
    """Tests for has_gpu() detection."""

    def test_env_override_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_HAS_GPU", "true")
        assert has_gpu() is True

    def test_env_override_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_HAS_GPU", "false")
        assert has_gpu() is False

    def test_no_nvidia_smi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_HAS_GPU", raising=False)
        with patch("shutil.which", return_value=None):
            assert has_gpu() is False

    def test_nvidia_smi_found_and_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_HAS_GPU", raising=False)
        with (
            patch("shutil.which", return_value="/usr/bin/nvidia-smi"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "NVIDIA GeForce RTX 3090"
            assert has_gpu() is True

    def test_nvidia_smi_found_but_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_HAS_GPU", raising=False)
        with (
            patch("shutil.which", return_value="/usr/bin/nvidia-smi"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert has_gpu() is False


# ---------------------------------------------------------------------------
# detect_capabilities()
# ---------------------------------------------------------------------------


class TestDetectCapabilities:
    """Tests for detect_capabilities() integration."""

    def setup_method(self) -> None:
        """Clear lru_cache before each test."""
        detect_capabilities.cache_clear()

    def test_integration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_MEMORY_MB", "2048")
        monkeypatch.setenv("NEXUS_CPU_CORES", "4")
        monkeypatch.setenv("NEXUS_HAS_GPU", "false")

        caps = detect_capabilities()
        assert caps.memory_mb == 2048
        assert caps.cpu_cores == 4
        assert caps.has_gpu is False
        assert caps.has_network is True

    def test_logs_timing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("NEXUS_MEMORY_MB", "1024")
        monkeypatch.setenv("NEXUS_CPU_CORES", "2")
        monkeypatch.setenv("NEXUS_HAS_GPU", "false")

        with caplog.at_level(logging.INFO, logger="nexus.lib.device_capabilities"):
            detect_capabilities()

        assert any("detected in" in record.message for record in caplog.records)

    def test_caching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_MEMORY_MB", "2048")
        monkeypatch.setenv("NEXUS_CPU_CORES", "4")
        monkeypatch.setenv("NEXUS_HAS_GPU", "false")

        caps1 = detect_capabilities()
        caps2 = detect_capabilities()
        assert caps1 is caps2  # same object from cache


# ---------------------------------------------------------------------------
# BrickRequirement + BRICK_REQUIREMENTS
# ---------------------------------------------------------------------------


class TestBrickRequirement:
    """Tests for BrickRequirement dataclass and BRICK_REQUIREMENTS mapping."""

    def test_frozen(self) -> None:
        req = BrickRequirement(min_memory_mb=512)
        with pytest.raises(AttributeError):
            req.min_memory_mb = 1024  # type: ignore[misc]

    def test_defaults(self) -> None:
        req = BrickRequirement()
        assert req.min_memory_mb == 0
        assert req.requires_gpu is False
        assert req.requires_network is False

    def test_all_bricks_have_entries(self) -> None:
        from nexus.contracts.deployment_profile import ALL_BRICK_NAMES

        for brick_name in ALL_BRICK_NAMES:
            assert brick_name in BRICK_REQUIREMENTS, f"Missing requirement for {brick_name}"

    def test_values_reasonable(self) -> None:
        for brick_name, req in BRICK_REQUIREMENTS.items():
            assert req.min_memory_mb >= 0, f"{brick_name} has negative memory requirement"
            assert req.min_memory_mb <= 4096, f"{brick_name} requires too much memory"


# ---------------------------------------------------------------------------
# bricks_for_device() — table-driven
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "memory_mb,has_gpu_flag,has_network_flag,expected_absent,expected_present",
    [
        pytest.param(
            256,
            False,
            True,
            {"search", "llm"},
            {"eventlog", "pay", "sandbox", "workflows"},
            id="256MB-no-gpu",
        ),
        pytest.param(
            2048,
            False,
            True,
            set(),
            {"search", "llm", "sandbox", "workflows", "pay"},
            id="2048MB-no-gpu-lite",
        ),
        pytest.param(
            8192,
            True,
            True,
            set(),
            {"search", "llm", "sandbox", "pay", "workflows", "federation"},
            id="8192MB-gpu-full",
        ),
        pytest.param(
            32768,
            True,
            True,
            set(),
            {"search", "llm", "sandbox", "pay", "workflows", "federation"},
            id="32768MB-gpu-cloud",
        ),
        pytest.param(
            512,
            False,
            True,
            {"llm"},
            {"search", "sandbox"},
            id="512MB-boundary",
        ),
        pytest.param(
            1024,
            False,
            False,
            {"pay", "federation"},
            {"search", "sandbox", "llm"},
            id="1024MB-no-network",
        ),
    ],
)
def test_bricks_for_device(
    memory_mb: int,
    has_gpu_flag: bool,
    has_network_flag: bool,
    expected_absent: set[str],
    expected_present: set[str],
) -> None:
    caps = DeviceCapabilities(
        memory_mb=memory_mb,
        has_gpu=has_gpu_flag,
        has_network=has_network_flag,
    )
    result = bricks_for_device(caps)
    for brick in expected_present:
        assert brick in result, f"{brick} should be present for {memory_mb}MB"
    for brick in expected_absent:
        assert brick not in result, f"{brick} should be absent for {memory_mb}MB"


# ---------------------------------------------------------------------------
# suggest_profile() — table-driven
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "memory_mb,expected_profile",
    [
        pytest.param(256, DeploymentProfile.EMBEDDED, id="256MB-embedded"),
        pytest.param(511, DeploymentProfile.EMBEDDED, id="511MB-embedded-boundary"),
        pytest.param(512, DeploymentProfile.LITE, id="512MB-lite"),
        pytest.param(1024, DeploymentProfile.LITE, id="1024MB-lite"),
        pytest.param(4095, DeploymentProfile.LITE, id="4095MB-lite-boundary"),
        pytest.param(4096, DeploymentProfile.FULL, id="4096MB-full"),
        pytest.param(8192, DeploymentProfile.FULL, id="8192MB-full"),
        pytest.param(32767, DeploymentProfile.FULL, id="32767MB-full-boundary"),
        pytest.param(32768, DeploymentProfile.CLOUD, id="32768MB-cloud"),
        pytest.param(65536, DeploymentProfile.CLOUD, id="65536MB-cloud"),
    ],
)
def test_suggest_profile(memory_mb: int, expected_profile: DeploymentProfile) -> None:
    caps = DeviceCapabilities(memory_mb=memory_mb)
    assert suggest_profile(caps) == expected_profile


# ---------------------------------------------------------------------------
# warn_if_profile_exceeds_device()
# ---------------------------------------------------------------------------


class TestWarnIfProfileExceedsDevice:
    """Tests for profile mismatch warnings."""

    def test_warns_when_profile_exceeds(self, caplog: pytest.LogCaptureFixture) -> None:
        caps = DeviceCapabilities(memory_mb=256)  # → EMBEDDED
        with caplog.at_level(logging.WARNING, logger="nexus.lib.device_capabilities"):
            warn_if_profile_exceeds_device(DeploymentProfile.FULL, caps)

        assert any("may exceed device capabilities" in r.message for r in caplog.records)

    def test_no_warning_when_appropriate(self, caplog: pytest.LogCaptureFixture) -> None:
        caps = DeviceCapabilities(memory_mb=8192)  # → FULL
        with caplog.at_level(logging.WARNING, logger="nexus.lib.device_capabilities"):
            warn_if_profile_exceeds_device(DeploymentProfile.LITE, caps)

        assert not any("may exceed" in r.message for r in caplog.records)

    def test_no_warning_when_exact_match(self, caplog: pytest.LogCaptureFixture) -> None:
        caps = DeviceCapabilities(memory_mb=8192)  # → FULL
        with caplog.at_level(logging.WARNING, logger="nexus.lib.device_capabilities"):
            warn_if_profile_exceeds_device(DeploymentProfile.FULL, caps)

        assert not any("may exceed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Smoke test — unmocked
# ---------------------------------------------------------------------------


class TestSmokeTest:
    """Smoke test: call detect_capabilities() without mocks."""

    def test_real_detection(self) -> None:
        detect_capabilities.cache_clear()
        caps = detect_capabilities()
        assert caps.memory_mb > 0
        assert caps.cpu_cores > 0
        assert isinstance(caps.has_gpu, bool)
