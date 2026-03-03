"""BlobTransport implementations for cloud storage providers.

Each transport provides raw key→blob I/O for a specific cloud backend.
Transports are shared between CAS and Path addressing engines.
"""

__all__ = [
    "GCSBlobTransport",
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
