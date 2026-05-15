"""Transport implementations for storage providers.

Each transport provides raw key→blob I/O for a specific storage backend.
Transports are shared between CAS and Path addressing engines.
"""

from nexus.backends.transports.local_transport import LocalTransport

__all__ = [
    "GCSTransport",
    "LocalTransport",
    "S3Transport",
]


# Lazy imports to avoid requiring cloud SDKs at import time
def __getattr__(name: str) -> object:
    if name == "GCSTransport":
        from nexus.backends.transports.gcs_transport import GCSTransport

        globals()["GCSTransport"] = GCSTransport
        return GCSTransport
    if name == "S3Transport":
        from nexus.backends.transports.s3_transport import S3Transport

        globals()["S3Transport"] = S3Transport
        return S3Transport
    raise AttributeError(f"module 'nexus.backends.transports' has no attribute {name}")
