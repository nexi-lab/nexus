"""API v2 - Memory REST endpoints.

This module exposes REST endpoints for the Nexus Memory
system under the /api/v2/ prefix.

Endpoint groups:
- /api/v2/memories - Memory CRUD and search
"""

from nexus.server.api.v2.routers import (
    memories,
)

__all__ = [
    "memories",
]
