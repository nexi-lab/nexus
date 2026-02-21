"""Access Manifest brick — declarative MCP tool scoping (Issue #1754).

Provides:
- ManifestEvaluator: Pure-function tool permission evaluation
- AccessManifestService: DB-backed manifest CRUD + ReBAC integration
"""

from nexus.bricks.access_manifest.evaluator import ManifestEvaluator
from nexus.bricks.access_manifest.service import AccessManifestService

__all__ = [
    "AccessManifestService",
    "ManifestEvaluator",
]
