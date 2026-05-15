from __future__ import annotations

from collections.abc import Mapping

INDEX_TTL_BY_BACKEND = {
    "local": 0,
    "path_local": 0,
    "disk": 60,
    "path_s3": 600,
    "path_gcs": 600,
    "github_connector": 600,
}


def index_ttl_for_backend(
    backend_id: str,
    overrides: Mapping[str, int] | None = None,
) -> int:
    if overrides and backend_id in overrides:
        return overrides[backend_id]
    return INDEX_TTL_BY_BACKEND.get(backend_id, 60)


def negative_ttl_for_backend(
    backend_id: str,
    overrides: Mapping[str, int] | None = None,
) -> int:
    return min(5, index_ttl_for_backend(backend_id, overrides))
