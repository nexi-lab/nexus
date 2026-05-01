"""Extensions endpoint — runtime introspection over the unified manifest layer.

Issue #3962: Cross-kind metadata for plugins, connectors, and bricks served
via the same store the CLI uses. Lazy: never imports an extension impl module.
"""

from __future__ import annotations

import logging
from typing import Any, cast, get_args

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from nexus.extensions.introspect import (
    check_extension,
    get_extension,
    list_extensions,
)
from nexus.extensions.types import Kind

logger = logging.getLogger(__name__)

router = APIRouter(tags=["extensions"], prefix="/api/v2/extensions")

_KINDS: tuple[str, ...] = get_args(Kind)


class CheckReportResponse(BaseModel):
    name: str
    kind: str
    available: bool
    missing_python_deps: list[str] = Field(default_factory=list)
    missing_binary_deps: list[str] = Field(default_factory=list)
    missing_services: list[str] = Field(default_factory=list)
    import_probe_failures: list[str] = Field(default_factory=list)
    profile_gate_disabled: bool = False


def _validate_kind(kind: str | None) -> Kind | None:
    if kind is None:
        return None
    if kind not in _KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind '{kind}'")
    return cast(Kind, kind)


@router.get("")
async def list_endpoint(
    kind: str | None = Query(default=None),
    profile: list[str] | None = Query(default=None),
    available_only: bool = Query(default=False),
) -> list[dict[str, Any]]:
    """List registered extensions, optionally filtered by kind/profile/availability."""
    k = _validate_kind(kind)
    profile_set = frozenset(profile) if profile else None
    manifests = list_extensions(kind=k, profile=profile_set, available_only=available_only)
    return [m.model_dump(mode="json") for m in manifests]


@router.get("/kinds")
async def kinds_endpoint() -> list[str]:
    """Return the registered extension kinds."""
    return list(_KINDS)


@router.get("/{kind}/{name}")
async def info_endpoint(kind: str, name: str) -> dict[str, Any]:
    """Return the full manifest for one (kind, name)."""
    k = _validate_kind(kind)
    assert k is not None  # narrowed by _validate_kind raising
    try:
        manifest = get_extension(name, kind=k)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return manifest.model_dump(mode="json")


@router.get("/{kind}/{name}/check", response_model=CheckReportResponse)
async def check_endpoint(kind: str, name: str) -> CheckReportResponse:
    """Run dependency probes for one (kind, name) and return a CheckReport."""
    k = _validate_kind(kind)
    assert k is not None
    try:
        report = check_extension(name, kind=k)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CheckReportResponse(
        name=name,
        kind=kind,
        available=report.available,
        missing_python_deps=list(report.missing_python_deps),
        missing_binary_deps=list(report.missing_binary_deps),
        missing_services=list(report.missing_services),
        import_probe_failures=list(report.import_probe_failures),
        profile_gate_disabled=report.profile_gate_disabled,
    )
