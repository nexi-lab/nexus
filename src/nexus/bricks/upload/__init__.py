"""Upload service domain -- BRICK tier.

Canonical location for resumable upload services.
"""

from nexus.bricks.upload.chunked_upload_service import ChunkedUploadService

__all__ = ["ChunkedUploadService"]
