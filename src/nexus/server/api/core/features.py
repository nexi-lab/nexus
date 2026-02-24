"""Features endpoint — runtime introspection of deployment profile.

Issue #1389: Clients can query which bricks/features are enabled
to adapt their UI and behavior dynamically.

The response is computed once at startup (immutable after boot)
and served from app.state.features_info with O(1) cost.
"""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from nexus.server.rate_limiting import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["features"])


class PerformanceTuningInfo(BaseModel):
    """Summarized performance tuning for features endpoint (Issue #2071)."""

    thread_pool_size: int = Field(description="AnyIO limiter tokens")
    default_workers: int = Field(description="General-purpose worker count")
    task_runner_workers: int = Field(description="Durable task queue workers")
    default_http_timeout: float = Field(description="Default HTTP timeout (seconds)")
    db_pool_size: int = Field(description="DB connection pool size")
    search_max_concurrency: int = Field(description="Search indexing concurrency")
    heartbeat_flush_interval: int = Field(description="Heartbeat flush interval (seconds)")
    default_max_retries: int = Field(description="Default retry count")
    blob_operation_timeout: float = Field(description="Blob storage operation timeout (seconds)")
    asyncpg_max_size: int = Field(description="AsyncPG connection pool max size")


class FeaturesResponse(BaseModel):
    """Response model for the features endpoint."""

    profile: str = Field(description="Active deployment profile (embedded/lite/full/cloud)")
    mode: str = Field(description="Deployment topology (standalone/remote/federation)")
    enabled_bricks: list[str] = Field(description="List of enabled brick names")
    disabled_bricks: list[str] = Field(description="List of disabled brick names")
    version: str | None = Field(default=None, description="Nexus version")
    performance_tuning: PerformanceTuningInfo | None = Field(
        default=None, description="Active performance tuning (Issue #2071)"
    )
    rate_limit_enabled: bool = Field(
        default=False, description="Whether rate limiting is active on this server"
    )


@router.get("/api/v2/features", response_model=FeaturesResponse)
@limiter.exempt
async def get_features(request: Request) -> FeaturesResponse:
    """Return the active deployment profile and enabled bricks.

    This endpoint is public (no auth required) to enable client
    capability discovery before authentication.
    """
    features_info: FeaturesResponse | None = getattr(request.app.state, "features_info", None)
    if features_info is not None:
        return features_info

    # Fallback: compute on the fly if not pre-computed
    return FeaturesResponse(
        profile="full",
        mode="standalone",
        enabled_bricks=[],
        disabled_bricks=[],
        version=None,
    )
