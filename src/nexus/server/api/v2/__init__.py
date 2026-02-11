"""API v2 - Comprehensive Memory & ACE REST endpoints.

This module exposes 37 REST endpoints for the Nexus Memory and ACE
(Agentic Context Engineering) systems under the /api/v2/ prefix.

Endpoint groups:
- /api/v2/memories - Memory CRUD and search (14 endpoints)
- /api/v2/trajectories - Trajectory tracking (5 endpoints)
- /api/v2/feedback - Feedback management (5 endpoints)
- /api/v2/playbooks - Playbook management (6 endpoints)
- /api/v2/reflect - Reflection analysis (1 endpoint)
- /api/v2/curate - Curation (2 endpoints)
- /api/v2/consolidate - Memory consolidation (4 endpoints)
"""

from nexus.server.api.v2.routers import (
    consolidation,
    curate,
    feedback,
    memories,
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
]
