from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilePathInvalidation:
    backend_id: str
    scope_id: str
    path: str
    namespace: str = "raw"


@dataclass(frozen=True)
class ParentListingInvalidation:
    backend_id: str
    scope_id: str
    path: str
