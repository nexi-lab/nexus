"""API v2 routers."""

from nexus.server.api.v2.routers import (
    consolidation,
    curate,
    feedback,
    memories,
    mobile_search,
    pay,
    playbooks,
    reflect,
    trajectories,
)

__all__ = [
    "memories",
    "trajectories",
    "feedback",
    "playbooks",
    "reflect",
    "curate",
    "consolidation",
    "mobile_search",
    "pay",
]
