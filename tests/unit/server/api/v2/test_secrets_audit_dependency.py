"""``get_secrets_audit_logger`` resolves itself — no production dependency override.

A non-empty ``app.dependency_overrides`` makes FastAPI re-run
``get_dependant`` (pydantic schema builds) for every sub-dependency of every
route on every request.  The secrets-audit router used to be the one entry
in that map, costing ~20% of server CPU on the search hot path.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from nexus.server.api.v2.routers.secrets_audit import get_secrets_audit_logger


def _request(**state: Any) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def test_non_admin_is_forbidden() -> None:
    with pytest.raises(HTTPException) as exc:
        get_secrets_audit_logger(_request(record_store=object()), {"is_admin": False})
    assert exc.value.status_code == 403


def test_admin_without_record_store_is_not_configured() -> None:
    with pytest.raises(HTTPException) as exc:
        get_secrets_audit_logger(_request(), {"is_admin": True})
    assert exc.value.status_code == 500


def test_admin_gets_one_cached_logger_and_their_zone() -> None:
    request = _request(record_store=SimpleNamespace(session_factory=lambda: None))
    first, zone = get_secrets_audit_logger(request, {"is_admin": True, "zone_id": "eng"})
    second, root_zone = get_secrets_audit_logger(request, {"is_admin": True})
    assert first is second
    assert zone == "eng"
    assert root_zone == "root"


def test_server_source_registers_no_dependency_overrides() -> None:
    src = Path(__file__).resolve().parents[5] / "src" / "nexus"
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        # In-tree test modules may override dependencies; server code may not.
        if "tests" not in path.relative_to(src).parts
        and not path.name.startswith("test_")
        and re.search(r"\.dependency_overrides\[[^\]]+\]\s*=", path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"production dependency_overrides slow every route: {offenders}"
