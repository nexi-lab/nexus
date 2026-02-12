"""Tests for Mobile Search REST API router.

Tests for issue #1213: Mobile/Edge Search Utilities.
Covers all 2 endpoints: detect, download.

Uses mocked mobile_config to test router logic in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.mobile_search import router

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_psutil():
    """Mock psutil.virtual_memory with sensible defaults."""
    mock_mem = MagicMock()
    mock_mem.total = 8 * (1024**3)  # 8 GB
    mock_mem.available = 4 * (1024**3)  # 4 GB
    mock_mem.percent = 50.0
    return mock_mem


@pytest.fixture
def mock_device_tier():
    """Mock DeviceTier enum value."""
    from nexus.search.mobile_config import DeviceTier

    return DeviceTier.MEDIUM


@pytest.fixture
def mock_tier_config():
    """Mock config returned by get_config_for_tier."""
    config = MagicMock()
    config.mode = MagicMock()
    config.mode.value = "hybrid_reranked"
    config.embedding = MagicMock()
    config.embedding.name = "nomic-ai/nomic-embed-text-v1.5"
    config.reranker = MagicMock()
    config.reranker.name = "jinaai/jina-reranker-v1-tiny-en"
    config.max_memory_mb = 150
    return config


@pytest.fixture
def app():
    """Create test FastAPI app with mobile search router."""
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.fixture
def client(app):
    """Test client."""
    return TestClient(app, raise_server_exceptions=False)


# =============================================================================
# Test: GET /api/v2/mobile/detect
# =============================================================================


class TestDetectDevice:
    def test_detect_success_with_psutil(
        self, client, mock_psutil, mock_device_tier, mock_tier_config
    ):
        with (
            patch(
                "nexus.server.api.v2.routers.mobile_search.detect_device_tier",
                return_value=mock_device_tier,
            ),
            patch(
                "nexus.server.api.v2.routers.mobile_search.get_config_for_tier",
                return_value=mock_tier_config,
            ),
            patch.dict("sys.modules", {"psutil": MagicMock()}),
            patch(
                "psutil.virtual_memory",
                return_value=mock_psutil,
            ),
        ):
            response = client.get("/api/v2/mobile/detect")

        assert response.status_code == 200
        data = response.json()
        assert data["detected_tier"] == "medium"
        assert "device" in data
        assert "recommended_config" in data
        assert data["recommended_config"]["mode"] == "hybrid_reranked"
        assert data["recommended_config"]["max_memory_mb"] == 150

    def test_detect_fallback_without_psutil(self, client, mock_device_tier, mock_tier_config):
        """When psutil is not installed, uses fallback values."""
        with (
            patch(
                "nexus.server.api.v2.routers.mobile_search.detect_device_tier",
                return_value=mock_device_tier,
            ),
            patch(
                "nexus.server.api.v2.routers.mobile_search.get_config_for_tier",
                return_value=mock_tier_config,
            ),
            patch(
                "builtins.__import__",
                side_effect=_import_error_for_psutil,
            ),
        ):
            response = client.get("/api/v2/mobile/detect")

        assert response.status_code == 200
        data = response.json()
        assert data["detected_tier"] == "medium"
        # Fallback values
        assert data["device"]["total_ram_gb"] == 8.0
        assert data["device"]["available_ram_gb"] == 4.0

    def test_detect_response_structure(
        self, client, mock_device_tier, mock_tier_config, mock_psutil
    ):
        """Verify full response structure matches expected shape."""
        with (
            patch(
                "nexus.server.api.v2.routers.mobile_search.detect_device_tier",
                return_value=mock_device_tier,
            ),
            patch(
                "nexus.server.api.v2.routers.mobile_search.get_config_for_tier",
                return_value=mock_tier_config,
            ),
            patch.dict("sys.modules", {"psutil": MagicMock()}),
            patch("psutil.virtual_memory", return_value=mock_psutil),
        ):
            response = client.get("/api/v2/mobile/detect")

        data = response.json()
        # Top-level keys
        assert set(data.keys()) == {"detected_tier", "device", "recommended_config"}
        # Device keys
        assert set(data["device"].keys()) == {
            "total_ram_gb",
            "available_ram_gb",
            "ram_usage_percent",
        }
        # Config keys
        assert set(data["recommended_config"].keys()) == {
            "tier",
            "mode",
            "embedding_model",
            "reranker_model",
            "max_memory_mb",
        }


# =============================================================================
# Test: POST /api/v2/mobile/download
# =============================================================================


class TestDownloadModels:
    def test_download_minimal_tier_no_models(self, client):
        """MINIMAL tier needs no model downloads."""
        response = client.post(
            "/api/v2/mobile/download",
            json={"tier": "minimal"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["models_downloaded"] == {}
        assert "no models needed" in data["message"].lower()

    def test_download_server_tier_no_models(self, client):
        """SERVER tier needs no local model downloads."""
        response = client.post(
            "/api/v2/mobile/download",
            json={"tier": "server"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["models_downloaded"] == {}

    def test_download_invalid_tier(self, client):
        """Invalid tier returns 400."""
        response = client.post(
            "/api/v2/mobile/download",
            json={"tier": "invalid_tier"},
        )
        assert response.status_code == 400
        assert "Invalid tier" in response.json()["detail"]

    def test_download_low_tier_success(self, client):
        """Successful model download for low tier."""
        mock_results = {"minishlab/potion-base-8M": True}
        with patch(
            "nexus.search.mobile_providers.download_models_for_tier",
            new_callable=AsyncMock,
            return_value=mock_results,
        ):
            response = client.post(
                "/api/v2/mobile/download",
                json={"tier": "low"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["models_downloaded"] == mock_results

    def test_download_partial_failure(self, client):
        """Some models fail to download."""
        mock_results = {
            "model-a": True,
            "model-b": False,
        }
        with patch(
            "nexus.search.mobile_providers.download_models_for_tier",
            new_callable=AsyncMock,
            return_value=mock_results,
        ):
            response = client.post(
                "/api/v2/mobile/download",
                json={"tier": "medium"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "failed" in data["message"].lower()

    def test_download_provider_not_installed(self, client):
        """Provider import fails returns 503."""
        with patch(
            "nexus.search.mobile_providers.download_models_for_tier",
            side_effect=ImportError("No module named 'fastembed'"),
        ):
            response = client.post(
                "/api/v2/mobile/download",
                json={"tier": "medium"},
            )
        assert response.status_code == 503
        assert "not installed" in response.json()["detail"].lower()

    def test_download_unexpected_error(self, client):
        """Unexpected error returns 500."""
        with patch(
            "nexus.search.mobile_providers.download_models_for_tier",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Disk full"),
        ):
            response = client.post(
                "/api/v2/mobile/download",
                json={"tier": "high"},
            )
        assert response.status_code == 500
        assert "Disk full" in response.json()["detail"]

    def test_download_missing_tier_field(self, client):
        """Missing required 'tier' field returns 422."""
        response = client.post(
            "/api/v2/mobile/download",
            json={},
        )
        assert response.status_code == 422


# =============================================================================
# Helpers
# =============================================================================


_original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__


def _import_error_for_psutil(name, *args, **kwargs):
    """Raise ImportError only for psutil."""
    if name == "psutil":
        raise ImportError("No module named 'psutil'")
    return _original_import(name, *args, **kwargs)
