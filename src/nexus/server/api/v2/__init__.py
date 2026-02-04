"""API v2 - Comprehensive Memory & ACE REST endpoints.

This module exposes 30 REST endpoints for the Nexus Memory and ACE
(Agentic Context Engineering) systems under the /api/v2/ prefix.

Endpoint groups:
- /api/v2/memories - Memory CRUD and search (7 endpoints)
- /api/v2/trajectories - Trajectory tracking (5 endpoints)
- /api/v2/feedback - Feedback management (5 endpoints)
- /api/v2/playbooks - Playbook management (6 endpoints)
- /api/v2/reflect, /api/v2/curate - Reflection & curation (3 endpoints)
- /api/v2/consolidate - Memory consolidation (4 endpoints)
"""

from nexus.server.api.v2.routers import (
    consolidation,
    feedback,
    memories,
    playbooks,
    reflection,
    trajectories,
)

__all__ = [
    "memories",
    "trajectories",
    "feedback",
    "playbooks",
    "reflection",
    "consolidation",
]
