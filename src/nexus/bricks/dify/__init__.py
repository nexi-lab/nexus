"""Dify Enterprise Permission Integration Brick.

Connects Nexus's cloud drive and ReBAC permission system to Dify's RAG pipeline,
ensuring Dify only retrieves documents the current user is authorized to access.

Three integration modes:
1. External Knowledge API — Nexus acts as a retrieval backend for Dify
2. Metadata Sync — pushes permission metadata to Dify knowledge bases
3. Hybrid — combines both for maximum flexibility

Architecture::

    Cloud Drive User ──► Nexus (ReBAC permissions)
                              │
                    ┌─────────┴──────────┐
                    ▼                    ▼
         External Knowledge API    Metadata Sync
         (Dify calls Nexus to     (Nexus pushes permission
          retrieve documents)      tags to Dify KB)
                    │                    │
                    └─────────┬──────────┘
                              ▼
                        Dify Chatflow
                    (permission-aware RAG)

Security: All retrieval requests are gated by Nexus ReBAC checks.
Dify never sees documents the requesting user cannot access.
"""

from nexus.bricks.dify.permission_bridge import DifyPermissionBridge
from nexus.bricks.dify.retrieval_service import DifyRetrievalService
from nexus.bricks.dify.sync_service import DifySyncService
from nexus.bricks.dify.types import DifyConfig

__all__ = [
    "DifyConfig",
    "DifyPermissionBridge",
    "DifyRetrievalService",
    "DifySyncService",
]
