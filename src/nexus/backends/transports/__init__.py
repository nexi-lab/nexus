"""BlobTransport implementations for storage providers.

Each transport provides raw key→blob I/O for a specific storage backend.
Transports are shared between CAS and Path addressing engines.
"""

from nexus.backends.transports.local_transport import LocalBlobTransport

__all__ = [
    "GCSBlobTransport",
    "LocalBlobTransport",
    "S3BlobTransport",
]


# Lazy imports to avoid requiring cloud SDKs at import time
def __getattr__(name: str) -> object:
    if name == "GCSBlobTransport":
        from nexus.backends.transports.gcs_transport import GCSBlobTransport

        globals()["GCSBlobTransport"] = GCSBlobTransport
        return GCSBlobTransport
    if name == "S3BlobTransport":
        from nexus.backends.transports.s3_transport import S3BlobTransport

        globals()["S3BlobTransport"] = S3BlobTransport
        return S3BlobTransport
    raise AttributeError(f"module 'nexus.backends.transports' has no attribute {name}")
