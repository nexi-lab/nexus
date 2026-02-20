"""Backward-compat shim — canonical: ``nexus.services.upload.chunked_upload_service``.

Issue #2132: Organized into domain subdirectory.
"""

import importlib
import warnings
from typing import Any

_MOVED = {
    "ChunkedUploadService": "nexus.services.upload.chunked_upload_service",
    "ChunkedUploadConfig": "nexus.services.upload.chunked_upload_service",
    "TUS_VERSION": "nexus.services.upload.chunked_upload_service",
    "TUS_EXTENSIONS": "nexus.services.upload.chunked_upload_service",
    "TUS_CHECKSUM_ALGORITHMS": "nexus.services.upload.chunked_upload_service",
    "DEFAULT_MAX_CONCURRENT": "nexus.services.upload.chunked_upload_service",
    "DEFAULT_SESSION_TTL_HOURS": "nexus.services.upload.chunked_upload_service",
    "DEFAULT_CLEANUP_INTERVAL_SECONDS": "nexus.services.upload.chunked_upload_service",
    "DEFAULT_MIN_CHUNK_SIZE": "nexus.services.upload.chunked_upload_service",
    "DEFAULT_MAX_CHUNK_SIZE": "nexus.services.upload.chunked_upload_service",
    "DEFAULT_CHUNK_SIZE": "nexus.services.upload.chunked_upload_service",
}


def __getattr__(name: str) -> Any:
    if name in _MOVED:
        warnings.warn(
            f"Importing {name} from {__name__} is deprecated. "
            f"Use 'from {_MOVED[name]} import {name}' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        mod = importlib.import_module(_MOVED[name])
        attr = getattr(mod, name)
        globals()[name] = attr  # Cache: warn once per process
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(_MOVED) + list(globals())
