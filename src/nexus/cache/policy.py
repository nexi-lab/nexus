from __future__ import annotations

INDEX_TTL_BY_BACKEND = {
    "local": 0,
    "path_local": 0,
    "disk": 60,
    "path_s3": 600,
    "path_gcs": 600,
    "github_connector": 600,
}


def index_ttl_for_backend(backend_id: str) -> int:
    return INDEX_TTL_BY_BACKEND.get(backend_id, 60)


def negative_ttl_for_backend(backend_id: str) -> int:
    return min(5, index_ttl_for_backend(backend_id))
