"""SearchService.list / glob must scan the caller's scoped prefix (#4740).

Regression for the "files are stored flat in standalone mode" strip: with
permissions enforced, a zone-scoped caller's ``/zone/<id>/…`` prefix was cut
back to ``/…`` before the metastore scan, so tenants' list/glob/grep looked at
the ROOT namespace and returned nothing.  The kernel stores rows under
``/zone/<id>/…``; the scan has to use that path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from nexus.bricks.search.search_service import SearchService
from nexus.contracts.types import OperationContext

ZONE = "za"
TENANT_FILE = f"/zone/{ZONE}/ws/f.txt"
ROOT_FILE = "/ws/root.txt"


class _FakeFS:
    """Records the readdir roots SearchService asks for; serves two rows."""

    def __init__(self) -> None:
        self.readdir_roots: list[str] = []
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

    def sys_readdir(self, path: str = "/", **kwargs: Any) -> list[dict[str, Any]]:
        self.readdir_roots.append(path)
        base = path.rstrip("/")
        return [dict(r) for r in self.rows if r["path"].startswith(base + "/")]


def _service(fs: _FakeFS) -> SearchService:
    enforcer = SimpleNamespace(
        filter_list=lambda paths, _ctx: list(paths),
        rebac_manager=None,
    )
    return SearchService(
        metadata_store=SimpleNamespace(),  # no ``_zone_id`` → standalone shape
        permission_enforcer=cast(Any, enforcer),
        enforce_permissions=True,
        nexus_fs=cast(Any, fs),
    )


def _tenant() -> OperationContext:
    return OperationContext(user_id="alice", groups=[], zone_id=ZONE)


def test_list_scans_the_scoped_prefix_not_the_root_namespace() -> None:
    fs = _FakeFS()
    svc = _service(fs)

    result = svc.list(f"/zone/{ZONE}/ws", recursive=True, context=_tenant())

    assert fs.readdir_roots, "list() never reached the syscall surface"
    assert all(root.startswith(f"/zone/{ZONE}/ws") for root in fs.readdir_roots), fs.readdir_roots
    assert TENANT_FILE in result
    assert ROOT_FILE not in result


def test_glob_matches_under_the_scoped_prefix() -> None:
    fs = _FakeFS()
    svc = _service(fs)

    matches = svc.glob("**/*.txt", f"/zone/{ZONE}/ws", context=_tenant())

    assert matches == [TENANT_FILE], matches
    assert all(root.startswith(f"/zone/{ZONE}") for root in fs.readdir_roots), fs.readdir_roots
