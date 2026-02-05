"""Mobile/Edge Search Utilities (Issue #1213).

Provides only the unique mobile-specific endpoints:
- GET  /api/v2/mobile/detect    - Detect device capabilities and recommended tier
- POST /api/v2/mobile/download  - Download models for offline use

For actual search operations, use the standard search APIs with the
appropriate embedding provider configured via MobileSearchConfig.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from nexus.search.mobile_config import (
    DeviceTier,
    detect_device_tier,
    get_config_for_tier,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/mobile", tags=["mobile"])


# =============================================================================
# Request/Response Models
# =============================================================================


class ModelDownloadRequest(BaseModel):
    """Request for model download."""

    tier: str = Field(..., description="Device tier to download models for")


class ModelDownloadResponse(BaseModel):
    """Response for model download."""

    success: bool
    models_downloaded: dict[str, bool] = Field(
        default_factory=dict, description="Model name -> download success"
    )
    message: str


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/detect")
async def detect_device() -> dict[str, Any]:
    """Detect device capabilities and recommended tier.

    Analyzes the current device's RAM and returns the recommended
    tier along with detected specifications.

    Returns:
        Device info and recommended configuration for search setup.

    Example response:
        {
            "detected_tier": "medium",
            "device": {
                "total_ram_gb": 8.0,
                "available_ram_gb": 4.2,
                "ram_usage_percent": 47.5
            },
            "recommended_config": {
                "tier": "medium",
                "mode": "hybrid_reranked",
                "embedding_model": "nomic-ai/nomic-embed-text-v1.5",
                "reranker_model": "jinaai/jina-reranker-v1-tiny-en",
                "max_memory_mb": 150
            }
        }
    """
    try:
        import psutil

        memory = psutil.virtual_memory()
        total_gb = memory.total / (1024**3)
        available_gb = memory.available / (1024**3)
        usage_percent = memory.percent
    except ImportError:
        # Fallback without psutil
        total_gb = 8.0  # Assume medium device
        available_gb = 4.0
        usage_percent = 50.0

    tier = detect_device_tier(total_ram_gb=total_gb, available_ram_gb=available_gb)
    config = get_config_for_tier(tier)

    return {
        "detected_tier": tier.value,
        "device": {
            "total_ram_gb": round(total_gb, 2),
            "available_ram_gb": round(available_gb, 2),
            "ram_usage_percent": round(usage_percent, 1),
        },
        "recommended_config": {
            "tier": tier.value,
            "mode": config.mode.value,
            "embedding_model": config.embedding.name if config.embedding else None,
            "reranker_model": config.reranker.name if config.reranker else None,
            "max_memory_mb": config.max_memory_mb,
        },
    }


@router.post("/download", response_model=ModelDownloadResponse)
async def download_models(request: ModelDownloadRequest) -> ModelDownloadResponse:
    """Download models for a specific device tier.

    Pre-downloads all models needed for offline operation at the specified tier.
    This is useful for mobile deployments where connectivity may be limited.

    Args:
        request: Tier to download models for (minimal, low, medium, high)

    Returns:
        Download status for each model

    Example:
        POST /api/v2/mobile/download
        {"tier": "low"}

        Response:
        {
            "success": true,
            "models_downloaded": {
                "minishlab/potion-base-8M": true
            },
            "message": "All models downloaded successfully"
        }
    """
    try:
        tier = DeviceTier(request.tier)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid tier: {request.tier}. Valid tiers: {[t.value for t in DeviceTier]}",
        ) from e

    # MINIMAL and SERVER tiers don't need model downloads
    if tier == DeviceTier.MINIMAL:
        return ModelDownloadResponse(
            success=True,
            models_downloaded={},
            message="MINIMAL tier uses keyword-only search, no models needed",
        )

    if tier == DeviceTier.SERVER:
        return ModelDownloadResponse(
            success=True,
            models_downloaded={},
            message="SERVER tier uses API providers, no local models needed",
        )

    try:
        from nexus.search.mobile_providers import download_models_for_tier

        results = await download_models_for_tier(tier.value)

        all_success = all(results.values()) if results else True
        message = (
            "All models downloaded successfully"
            if all_success
            else "Some models failed to download"
        )

        return ModelDownloadResponse(
            success=all_success,
            models_downloaded=results,
            message=message,
        )

    except ImportError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Provider not installed: {e}. Install with: pip install fastembed model2vec sentence-transformers",
        ) from e
    except Exception as e:
        logger.error(f"Model download failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Download failed: {e}",
        ) from e
