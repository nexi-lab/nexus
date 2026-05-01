"""Archive storage backends."""

from nexus.bricks.archive.storage.base import ArchiveStorage, StorageEntry
from nexus.bricks.archive.storage.local import LocalArchiveStorage

__all__ = ["ArchiveStorage", "StorageEntry", "LocalArchiveStorage"]

try:
    from nexus.bricks.archive.storage.s3 import S3ArchiveStorage  # noqa: F401

    __all__.append("S3ArchiveStorage")
except ImportError:
    pass  # boto3 optional in slim images
