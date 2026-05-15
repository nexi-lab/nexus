"""E2E tests for device capability auto-detection and profile suggestion.

Issue #1708: 'lite' deployment profile + DeviceCapabilities auto-detection.

Tests:
- Auto profile via features endpoint (mocked capabilities)
- Explicit profile with mismatch warning
- FeaturesConfig override with auto profile
"""

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.deployment_profile import (
    ALL_BRICK_NAMES,
    DeploymentProfile,
    resolve_enabled_bricks,
)
from nexus.lib.device_capabilities import DeviceCapabilities
from nexus.server.api.core.features import FeaturesResponse, router


def _make_features_app(
    profile: DeploymentProfile,
    enabled_bricks: frozenset[str] | None = None,
) -> FastAPI:
    """Create a FastAPI app with features endpoint and pre-computed features_info."""
    app = FastAPI()
    bricks = enabled_bricks if enabled_bricks is not None else profile.default_bricks()
    disabled = sorted(ALL_BRICK_NAMES - bricks)

    app.state.features_info = FeaturesResponse(
        profile=profile.value,
        mode="standalone",
        enabled_bricks=sorted(bricks),
        disabled_bricks=disabled,
        version="0.test.0",
    )
    app.state.limiter = MagicMock()
    app.include_router(router)
    return app


class TestAutoProfileViaFeatures:
    """Test auto-detected profile reflected in /api/v2/features."""

    @pytest.mark.parametrize(
        "memory_mb,expected_profile,expected_absent",
        [
            pytest.param(256, "embedded", {"search", "pay", "llm"}, id="256MB-embedded"),
            pytest.param(2048, "lite", {"search", "pay", "llm"}, id="2048MB-lite"),
            pytest.param(8192, "full", set(), id="8192MB-full"),
            pytest.param(65536, "cloud", set(), id="65536MB-cloud"),
        ],
    )
    def test_auto_detected_profile(
        self,
        memory_mb: int,
        expected_profile: str,
        expected_absent: set[str],
    ) -> None:
        from nexus.lib.device_capabilities import suggest_profile

        caps = DeviceCapabilities(memory_mb=memory_mb, cpu_cores=4, has_gpu=False)
        profile = suggest_profile(caps)

        assert profile.value == expected_profile

        bricks = resolve_enabled_bricks(profile)
        app = _make_features_app(profile, enabled_bricks=bricks)
        client = TestClient(app)

        data = client.get("/api/v2/features").json()
        assert data["profile"] == expected_profile
        for brick in expected_absent:
            assert brick not in data["enabled_bricks"]


class TestExplicitProfileWithMismatchWarning:
    """Test that explicit profile with low RAM logs a warning."""

    def test_mismatch_warning_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.lib.device_capabilities import warn_if_profile_exceeds_device

        caps = DeviceCapabilities(memory_mb=256)  # suggested: embedded
        with caplog.at_level(logging.WARNING, logger="nexus.lib.device_capabilities"):
            warn_if_profile_exceeds_device(DeploymentProfile.FULL, caps)

        assert any("may exceed device capabilities" in r.message for r in caplog.records)
        assert any("RAM=256MB" in r.message for r in caplog.records)

    def test_no_warning_for_matching_profile(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.lib.device_capabilities import warn_if_profile_exceeds_device

        caps = DeviceCapabilities(memory_mb=8192)  # suggested: full
        with caplog.at_level(logging.WARNING, logger="nexus.lib.device_capabilities"):
            warn_if_profile_exceeds_device(DeploymentProfile.FULL, caps)

        assert not any("may exceed" in r.message for r in caplog.records)


class TestFeaturesConfigOverrideWithAutoProfile:
    """Test that FeaturesConfig overrides work with auto-detected profile."""

    def test_auto_profile_with_search_override(self) -> None:
        """Auto-detect lite profile, then force-enable search via override."""
        from nexus.lib.device_capabilities import suggest_profile

        caps = DeviceCapabilities(memory_mb=2048)  # lite
        profile = suggest_profile(caps)
        assert profile == DeploymentProfile.LITE

        # Force-enable search via FeaturesConfig override
        bricks = resolve_enabled_bricks(profile, overrides={"search": True})
        assert "search" in bricks

        # Verify through features endpoint
        app = _make_features_app(profile, enabled_bricks=bricks)
        client = TestClient(app)

        data = client.get("/api/v2/features").json()
        assert data["profile"] == "lite"
        assert "search" in data["enabled_bricks"]

    def test_auto_profile_with_brick_disabled(self) -> None:
        """Auto-detect full profile, then force-disable pay via override."""
        from nexus.lib.device_capabilities import suggest_profile

        caps = DeviceCapabilities(memory_mb=8192)  # full
        profile = suggest_profile(caps)
        assert profile == DeploymentProfile.FULL

        # Force-disable pay via override
        bricks = resolve_enabled_bricks(profile, overrides={"pay": False})
        assert "pay" not in bricks

        app = _make_features_app(profile, enabled_bricks=bricks)
        client = TestClient(app)

        data = client.get("/api/v2/features").json()
        assert data["profile"] == "full"
        assert "pay" not in data["enabled_bricks"]
        assert "pay" in data["disabled_bricks"]


class TestComputeFeaturesInfoAutoProfile:
    """Test _compute_features_info with auto-resolved bricks."""

    def test_compute_with_lite_bricks(self) -> None:
        from nexus.server.lifespan import _compute_features_info
        from nexus.server.lifespan.services_container import LifespanServices

        app = FastAPI()
        app.state.deployment_profile = "lite"
        app.state.deployment_mode = "standalone"
        app.state.enabled_bricks = resolve_enabled_bricks(DeploymentProfile.LITE)

        svc = LifespanServices(
            deployment_profile="lite",
            deployment_mode="standalone",
            enabled_bricks=resolve_enabled_bricks(DeploymentProfile.LITE),
        )
        _compute_features_info(app, svc)

        info: Any = app.state.features_info
        assert info.profile == "lite"
        assert "search" not in info.enabled_bricks
        assert "search" in info.disabled_bricks
