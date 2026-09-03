"""``SearchService.list``: the wired default context is the embedded operator (#4740).

The factory wires ``SearchService(default_context=nx._init_cred)`` and the MCP
tools in sandbox mode pass that same credential explicitly.  It is zone-less
and non-admin, so the #4740 fail-closed rule refused the embedded operator —
the CI ``test_issue_4129_sandbox_search_e2e`` failure.  Identity with the
default context keeps the unrestricted view ``context=None`` gets; a copy
with the same fields is an ordinary zone-less caller and is refused.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.bricks.search.search_service import SearchService
from nexus.contracts.exceptions import PermissionDeniedError
from nexus.contracts.types import OperationContext
from nexus.core.pagination import PaginatedResult

TENANT_FILE = "/zone/za/ws/f.txt"
ROOT_FILE = "/ws/root.txt"


class _FakeFS:
    def __init__(self) -> None:
        self.rows = [
            {"path": TENANT_FILE, "size": 3, "entry_type": 0, "zone_id": "root"},
            {"path": ROOT_FILE, "size": 3, "entry_type": 0, "zone_id": "root"},
        ]

    def _get_context_identity(self, context: Any) -> tuple[str | None, str | None, bool]:
        if context is None:
            return ("root", None, True)
        return (context.zone_id, context.agent_id, context.is_admin)

    def sys_stat(self, path: str, **_: Any) -> dict[str, Any] | None:
        return None

    def sys_readdir(self, path: str = "/", **kwargs: Any) -> Any:
        base = path.rstrip("/")
        rows = [dict(r) for r in self.rows if r["path"].startswith(base + "/")]
        if kwargs.get("limit") is not None:  # the real syscall pages when asked to
            return PaginatedResult(items=rows, next_cursor=None, has_more=False)
        return rows


def _service(default_context: OperationContext) -> SearchService:
    enforcer = SimpleNamespace(filter_list=lambda paths, _ctx: list(paths), rebac_manager=None)
    return SearchService(
        metadata_store=SimpleNamespace(),  # no ``_zone_id`` → standalone shape
        permission_enforcer=cast(Any, enforcer),
        enforce_permissions=True,
        default_context=default_context,
        nexus_fs=cast(Any, _FakeFS()),
    )


def _process_cred() -> OperationContext:
    # What ``create_nexus_fs`` installs when no init_cred is given.
    return OperationContext(user_id="system", groups=[])


def _paths(items: Any) -> set[str]:
    return {i["path"] if isinstance(i, dict) else i for i in items}


def test_default_context_passed_explicitly_sees_everything() -> None:
    cred = _process_cred()
    svc = _service(cred)

    result = svc.list("/", recursive=True, context=cred)

    assert _paths(result) == {TENANT_FILE, ROOT_FILE}


def test_default_context_paginated_sees_everything() -> None:
    cred = _process_cred()
    svc = _service(cred)

    page = svc.list("/", recursive=True, context=cred, limit=10)

    assert _paths(page.items) == {TENANT_FILE, ROOT_FILE}


def test_copy_of_default_context_is_refused() -> None:
    cred = _process_cred()
    svc = _service(cred)

    with pytest.raises(PermissionDeniedError):
        svc.list("/", recursive=True, context=dataclasses.replace(cred))
