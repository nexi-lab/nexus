"""Zone visibility matrix — acceptance test for nexi-lab/nexus#4740.

Real kernel-backed NexusFS + the real FastAPI app + real API-key auth
(``DatabaseAPIKeyAuth`` for the tenant/admin keys, chained with a
``StaticAPIKeyAuth`` key that deliberately carries no zone).  No mocks on
the request path, so a regression in the RPC zone scoping, the REST zone
gate, ``sys_readdir`` or the search list pipeline shows up as a real HTTP
result.

Matrix (each cell over ``/api/nfs/list``, ``/api/v2/files/list``,
``/api/v2/search/glob`` and ``/api/v2/search/grep``):

* tenant A, with and without ``X-Nexus-Zone-ID`` — sees its own file, never
  tenant B's or the root-tagged file;
* tenant B — symmetric;
* tenant A asking for tenant B's zone via the header — rejected;
* a non-admin asking for ``all_zones`` — 403, not silently narrowed;
* a key with **no zone claim** (the issue's headline case) — 403 on list
  and search instead of the root/global view;
* admin without a zone header — the root zone only (root-tagged file, no
  tenant trees);
* admin with ``all_zones=true`` — the anti-vacuity control: sees tenant A,
  tenant B and the root-tagged file, and the access is audited exactly once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.config import PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.lib.events import register_audit_sink
from nexus.lib.zone_visibility import ALL_ZONES_AUDIT_EVENT
from nexus.server.auth.factory import _ChainedAPIKeyAuth
from nexus.storage.models import Base, ZoneModel
from nexus.storage.record_store import SQLAlchemyRecordStore

pytestmark = pytest.mark.e2e

RPC_PERMISSION_ERROR = -32003

ZONE_A = "ta"
ZONE_B = "tb"
FILE_A = "/a.txt"
FILE_B = "/b.txt"
FILE_ROOT = "/root.txt"
WORD_A = "alpha-tenant-a-secret"
WORD_B = "bravo-tenant-b-secret"
WORD_ROOT = "rootword-operator-only"
ZONELESS_KEY = "sk-zoneless-nonadmin-e2e-4740"


@dataclass
class Stack:
    client: TestClient
    nx: Any
    key_a: str
    key_b: str
    key_admin: str
    key_zoneless: str
    audit_events: list[tuple[str, dict[str, Any]]]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    tmp: Path = tmp_path_factory.mktemp("zone_visibility_matrix")
    mp = pytest.MonkeyPatch()
    mp.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    mp.setenv("NEXUS_SEARCH_DAEMON", "false")
    mp.setenv("NEXUS_ENABLE_WRITE_BUFFER", "false")
    mp.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    # The locally installed nexusd-cluster demands a bootstrap mode; the
    # pinned CI kernel ignores the variable.
    if "NEXUS_BOOTSTRAP_MODE" not in __import__("os").environ:
        mp.setenv("NEXUS_BOOTSTRAP_MODE", "static")

    storage = tmp / "storage"
    storage.mkdir()
    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{tmp / 'records.db'}")
    try:
        nx = create_nexus_fs(
            backend=CASLocalBackend(root_path=storage),
            metadata_store=str(tmp / "meta"),
            record_store=record_store,
            permissions=PermissionConfig(enforce=False),
            is_admin=False,
        )
    except Exception as exc:  # kernel spawn failure surfaces as AttributeError/RuntimeError
        mp.undo()
        pytest.skip(f"kernel binary unavailable in this environment: {exc}")
    if getattr(nx, "_kernel", None) is None:
        nx.close()
        mp.undo()
        pytest.skip("kernel binary unavailable in this environment")

    # --- real DB-backed API keys: two tenants + one zone-less global admin ---
    engine = create_engine(f"sqlite:///{tmp / 'auth.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        session.add(ZoneModel(zone_id=ZONE_A, name="Tenant A"))
        session.add(ZoneModel(zone_id=ZONE_B, name="Tenant B"))
        session.commit()
        _, key_a = DatabaseAPIKeyAuth.create_key(
            session, user_id="alice", name="alice-key", zone_id=ZONE_A
        )
        _, key_b = DatabaseAPIKeyAuth.create_key(
            session, user_id="bob", name="bob-key", zone_id=ZONE_B
        )
        _, key_admin = DatabaseAPIKeyAuth.create_key(
            session, user_id="root-op", name="admin-key", zone_id=None, is_admin=True
        )
        session.commit()

    # A static non-admin key with NO zone claim.  DatabaseAPIKeyAuth already
    # refuses such keys at authentication time (#3871), so the static
    # provider is the only way to reach the server with one — exactly the
    # "authenticated but zone-less" shape #4740 is about.
    static_provider = StaticAPIKeyAuth(
        {ZONELESS_KEY: {"subject_type": "user", "subject_id": "nozone", "is_admin": False}}
    )
    db_provider = DatabaseAPIKeyAuth(SimpleNamespace(session_factory=session_factory))
    provider = _ChainedAPIKeyAuth(static_provider, db_provider)

    from nexus.server.fastapi_server import create_app

    app = create_app(
        nexus_fs=nx,
        auth_provider=provider,
        database_url=f"sqlite:///{tmp / 'records.db'}",
    )

    audit_events: list[tuple[str, dict[str, Any]]] = []
    handle = register_audit_sink(lambda name, payload: audit_events.append((name, payload)))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            s = Stack(
                client=client,
                nx=nx,
                key_a=key_a,
                key_b=key_b,
                key_admin=key_admin,
                key_zoneless=ZONELESS_KEY,
                audit_events=audit_events,
            )
            _seed(s)
            yield s
    finally:
        handle.remove()
        nx.close()
        mp.undo()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _headers(key: str, zone: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {key}"}
    if zone is not None:
        h["X-Nexus-Zone-ID"] = zone
    return h


def _rpc(s: Stack, key: str, method: str, params: dict[str, Any], zone: str | None = None):
    return s.client.post(
        f"/api/nfs/{method}",
        json={"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params},
        headers=_headers(key, zone),
    )


def _rpc_result(resp) -> Any:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


def _rpc_error_code(resp) -> int | None:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body.get("error", {}).get("code") if "error" in body else None


def _collect_paths(payload: Any) -> set[str]:
    """Every path-like string in a list/glob/grep response, whatever the envelope."""
    out: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.startswith("/"):
                out.add(node)
        elif isinstance(node, dict):
            for k in ("path", "file", "file_path", "virtual_path"):
                v = node.get(k)
                if isinstance(v, str):
                    out.add(v)
            for k in ("files", "items", "results", "matches", "data"):
                if k in node:
                    walk(node[k])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return out


def _rpc_list(s: Stack, key: str, zone: str | None = None, **extra: Any) -> set[str]:
    return _collect_paths(_rpc_result(_rpc(s, key, "list", {"path": "/", **extra}, zone)))


def _rest_list(s: Stack, key: str, zone: str | None = None, **params: Any):
    return s.client.get(
        "/api/v2/files/list", params={"path": "/", **params}, headers=_headers(key, zone)
    )


def _glob(s: Stack, key: str, zone: str | None = None):
    return s.client.get(
        "/api/v2/search/glob",
        params={"pattern": "**/*.txt", "path": "/"},
        headers=_headers(key, zone),
    )


def _grep(s: Stack, key: str, zone: str | None = None):
    return s.client.get(
        "/api/v2/search/grep",
        params={"pattern": "secret|rootword", "path": "/"},
        headers=_headers(key, zone),
    )


def _seed(s: Stack) -> None:
    _rpc_result(_rpc(s, s.key_a, "write", {"path": FILE_A, "content": WORD_A}))
    _rpc_result(_rpc(s, s.key_b, "write", {"path": FILE_B, "content": WORD_B}))
    _rpc_result(_rpc(s, s.key_admin, "write", {"path": FILE_ROOT, "content": WORD_ROOT}))

    # The search router applies a file-level ReBAC filter to glob/grep hits
    # on top of the zone predicate (Decision #17), so give each principal a
    # read grant on the files it legitimately owns.  Without these the
    # tenant cells would be vacuous ("sees nothing" for the wrong reason);
    # the zone predicate is what must hide the OTHER tenants' files.
    # ReBAC objects are keyed by the user-facing path within the zone (the
    # enforcer unscopes ``/zone/<id>/`` before checking), hence ``/a.txt``
    # in zone ``ta``, not ``/zone/ta/a.txt``.
    rebac = s.nx.service("rebac")
    assert rebac is not None, "ReBAC service must be wired for the search cells"
    grants = [
        ("alice", FILE_A, ZONE_A),
        ("bob", FILE_B, ZONE_B),
        ("root-op", FILE_ROOT, ROOT_ZONE_ID),
        ("root-op", FILE_A, ZONE_A),
        ("root-op", FILE_B, ZONE_B),
    ]
    for user, path, zone in grants:
        rebac.rebac_create_sync(
            subject=("user", user),
            relation="direct_viewer",
            object=("file", path),
            zone_id=zone,
        )


def _assert_only_tenant(paths: set[str], *, own: str, others: tuple[str, ...]) -> None:
    assert any(p.endswith(own) for p in paths), f"own file {own} missing from {sorted(paths)}"
    leaked = [p for p in paths if any(p.endswith(o) for o in others)]
    assert not leaked, f"cross-tenant leak: {leaked}"


# ---------------------------------------------------------------------------
# tenant rows of the matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("zone_header", [None, ZONE_A], ids=["no-header", "own-zone-header"])
def test_tenant_a_list_sees_only_its_zone(stack: Stack, zone_header: str | None) -> None:
    others = (FILE_B, FILE_ROOT)
    _assert_only_tenant(_rpc_list(stack, stack.key_a, zone_header), own=FILE_A, others=others)

    rest = _rest_list(stack, stack.key_a, zone_header)
    assert rest.status_code == 200, rest.text
    rest_paths = _collect_paths(rest.json())
    leaked = [p for p in rest_paths if p.endswith(FILE_B) or p.endswith(FILE_ROOT)]
    assert not leaked, f"REST list leaked {leaked}"


@pytest.mark.parametrize("zone_header", [None, ZONE_A], ids=["no-header", "own-zone-header"])
def test_tenant_a_search_sees_only_its_zone(stack: Stack, zone_header: str | None) -> None:
    glob = _glob(stack, stack.key_a, zone_header)
    assert glob.status_code == 200, glob.text
    _assert_only_tenant(_collect_paths(glob.json()), own=FILE_A, others=(FILE_B, FILE_ROOT))

    grep = _grep(stack, stack.key_a, zone_header)
    assert grep.status_code == 200, grep.text
    body = grep.text
    assert WORD_A in body
    assert WORD_B not in body
    assert WORD_ROOT not in body


def test_tenant_b_is_symmetric(stack: Stack) -> None:
    _assert_only_tenant(_rpc_list(stack, stack.key_b), own=FILE_B, others=(FILE_A, FILE_ROOT))
    glob = _glob(stack, stack.key_b, ZONE_B)
    assert glob.status_code == 200, glob.text
    _assert_only_tenant(_collect_paths(glob.json()), own=FILE_B, others=(FILE_A, FILE_ROOT))


def test_tenant_cannot_borrow_another_zone_via_header(stack: Stack) -> None:
    resp = _rpc(stack, stack.key_a, "list", {"path": "/"}, zone=ZONE_B)
    # The zone override is rejected at authentication (401); if a deployment
    # ever lets it through, the visibility predicate must still hide B.
    if resp.status_code == 200 and "error" not in resp.json():
        paths = _collect_paths(resp.json()["result"])
        assert not any(p.endswith(FILE_B) for p in paths)
    else:
        assert resp.status_code in (401, 403), resp.text


def test_non_admin_all_zones_is_refused(stack: Stack) -> None:
    assert _rpc_error_code(_rpc(stack, stack.key_a, "list", {"path": "/", "all_zones": True})) == (
        RPC_PERMISSION_ERROR
    )
    rest = _rest_list(stack, stack.key_a, all_zones="true")
    assert rest.status_code == 403, rest.text


# ---------------------------------------------------------------------------
# the headline case: authenticated, non-admin, no zone claim → refused
# ---------------------------------------------------------------------------


def test_zone_less_non_admin_list_returns_403(stack: Stack) -> None:
    rest = _rest_list(stack, stack.key_zoneless)
    assert rest.status_code == 403, rest.text
    assert "zone" in rest.text.lower()

    code = _rpc_error_code(_rpc(stack, stack.key_zoneless, "list", {"path": "/"}))
    assert code == RPC_PERMISSION_ERROR

    code = _rpc_error_code(_rpc(stack, stack.key_zoneless, "sys_readdir", {"path": "/"}))
    assert code == RPC_PERMISSION_ERROR


def test_zone_less_non_admin_search_returns_403(stack: Stack) -> None:
    assert _glob(stack, stack.key_zoneless).status_code == 403
    assert _grep(stack, stack.key_zoneless).status_code == 403


def test_zone_less_non_admin_with_zone_header_is_scoped_not_global(stack: Stack) -> None:
    """Sending a zone header turns the zone-less key into a zone-scoped caller."""
    resp = _rpc(stack, stack.key_zoneless, "list", {"path": "/"}, zone=ZONE_A)
    if resp.status_code == 200 and "error" not in resp.json():
        paths = _collect_paths(resp.json()["result"])
        assert not any(p.endswith(FILE_B) or p.endswith(FILE_ROOT) for p in paths)
    else:
        # A deployment that refuses the override outright is also fail-closed.
        assert resp.status_code in (401, 403) or _rpc_error_code(resp) == RPC_PERMISSION_ERROR


# ---------------------------------------------------------------------------
# admin rows: root zone by default, everything only with all_zones (audited)
# ---------------------------------------------------------------------------


def test_admin_without_all_zones_sees_root_zone_only(stack: Stack) -> None:
    paths = _rpc_list(stack, stack.key_admin)
    assert FILE_ROOT in paths, sorted(paths)
    leaked = [p for p in paths if p.endswith(FILE_A) or p.endswith(FILE_B)]
    assert not leaked, f"admin default view leaked tenant data: {leaked}"

    rest = _rest_list(stack, stack.key_admin)
    assert rest.status_code == 200, rest.text
    rest_paths = _collect_paths(rest.json())
    assert FILE_ROOT in rest_paths, sorted(rest_paths)
    assert not any(p.endswith(FILE_A) or p.endswith(FILE_B) for p in rest_paths)


def test_admin_search_is_scoped_per_zone_header(stack: Stack) -> None:
    """Search has no ``all_zones``; the admin control is one zone per request."""
    glob_root = _glob(stack, stack.key_admin)
    assert glob_root.status_code == 200, glob_root.text
    _assert_only_tenant(_collect_paths(glob_root.json()), own=FILE_ROOT, others=(FILE_A, FILE_B))

    glob_a = _glob(stack, stack.key_admin, ZONE_A)
    assert glob_a.status_code == 200, glob_a.text
    _assert_only_tenant(_collect_paths(glob_a.json()), own=FILE_A, others=(FILE_B, FILE_ROOT))

    glob_b = _glob(stack, stack.key_admin, ZONE_B)
    assert glob_b.status_code == 200, glob_b.text
    _assert_only_tenant(_collect_paths(glob_b.json()), own=FILE_B, others=(FILE_A, FILE_ROOT))

    grep_b = _grep(stack, stack.key_admin, ZONE_B)
    assert grep_b.status_code == 200, grep_b.text
    assert WORD_B in grep_b.text
    assert WORD_A not in grep_b.text
    assert WORD_ROOT not in grep_b.text


# ---------------------------------------------------------------------------
# REST file routes: tenants operate on their own /zone/<id>/ namespace, like RPC
# ---------------------------------------------------------------------------


def _rest(s: Stack, key: str, method: str, url: str, zone: str | None = None, **kw: Any):
    return getattr(s.client, method)(url, headers=_headers(key, zone), **kw)


def test_rest_files_are_namespaced_per_tenant(stack: Stack) -> None:
    """REST write/read/exists/metadata/list/rename/delete all live in the tenant namespace."""
    w = _rest(
        stack,
        stack.key_a,
        "post",
        "/api/v2/files/write",
        json={"path": "/rest-a.txt", "content": "rest tenant a"},
    )
    assert w.status_code == 200, w.text
    revision = w.json().get("revision")
    assert revision and revision.startswith("/rest-a.txt@"), w.json()

    # stored under tenant A's namespace, not in the root namespace
    assert stack.nx.sys_stat(f"/zone/{ZONE_A}/rest-a.txt") is not None
    assert stack.nx.sys_stat("/rest-a.txt") is None

    # A reads it back (also through the revision fence); B cannot address it
    r = _rest(stack, stack.key_a, "get", "/api/v2/files/read", params={"path": "/rest-a.txt"})
    assert r.status_code == 200 and r.json()["content"] == "rest tenant a", r.text
    fenced = stack.client.get(
        "/api/v2/files/read",
        params={"path": "/rest-a.txt"},
        headers={**_headers(stack.key_a), "X-Nexus-Min-Revision": revision},
    )
    assert fenced.status_code == 200, fenced.text
    rb = _rest(stack, stack.key_b, "get", "/api/v2/files/read", params={"path": "/rest-a.txt"})
    assert rb.status_code == 404, rb.text
    ex_b = _rest(stack, stack.key_b, "get", "/api/v2/files/exists", params={"path": "/rest-a.txt"})
    assert ex_b.status_code == 200 and ex_b.json()["exists"] is False, ex_b.text

    ex_a = _rest(stack, stack.key_a, "get", "/api/v2/files/exists", params={"path": "/rest-a.txt"})
    assert ex_a.status_code == 200 and ex_a.json()["exists"] is True, ex_a.text
    meta = _rest(
        stack, stack.key_a, "get", "/api/v2/files/metadata", params={"path": "/rest-a.txt"}
    )
    assert meta.status_code == 200, meta.text
    assert not any("/zone/" in p for p in _collect_paths(meta.json()))

    # listing shows A's files under caller-facing paths and nothing else
    lst = _rest_list(stack, stack.key_a)
    assert lst.status_code == 200, lst.text
    paths = _collect_paths(lst.json())
    assert "/rest-a.txt" in paths and "/a.txt" in paths, sorted(paths)
    assert not any(p.endswith(FILE_B) or p.endswith(FILE_ROOT) or "/zone/" in p for p in paths)

    # explicit cross-zone paths are refused, never silently re-rooted
    for method, url, kw in (
        ("get", "/api/v2/files/read", {"params": {"path": f"/zone/{ZONE_B}{FILE_B}"}}),
        (
            "post",
            "/api/v2/files/write",
            {"json": {"path": f"/zone/{ZONE_B}/x.txt", "content": "x"}},
        ),
        ("get", "/api/v2/files/list", {"params": {"path": f"/zone/{ZONE_B}"}}),
    ):
        resp = _rest(stack, stack.key_a, method, url, **kw)
        assert resp.status_code == 403, (url, resp.status_code, resp.text)

    # rename and delete stay inside the namespace
    rn = _rest(
        stack,
        stack.key_a,
        "post",
        "/api/v2/files/rename",
        json={"source": "/rest-a.txt", "destination": "/rest-a2.txt"},
    )
    assert rn.status_code == 200, rn.text
    assert stack.nx.sys_stat(f"/zone/{ZONE_A}/rest-a2.txt") is not None
    d = _rest(stack, stack.key_a, "delete", "/api/v2/files/delete", params={"path": "/rest-a2.txt"})
    assert d.status_code == 200, d.text
    assert stack.nx.sys_stat(f"/zone/{ZONE_A}/rest-a2.txt") is None


def test_rest_files_glob_and_grep_are_namespaced(stack: Stack) -> None:
    glob = _rest(
        stack, stack.key_a, "get", "/api/v2/files/glob", params={"pattern": "**/*.txt", "path": "/"}
    )
    assert glob.status_code == 200, glob.text
    _assert_only_tenant(_collect_paths(glob.json()), own=FILE_A, others=(FILE_B, FILE_ROOT))
    assert not any("/zone/" in p for p in _collect_paths(glob.json()))

    grep = _rest(
        stack,
        stack.key_a,
        "get",
        "/api/v2/files/grep",
        params={"pattern": "secret|rootword", "path": "/"},
    )
    assert grep.status_code == 200, grep.text
    assert WORD_A in grep.text and WORD_B not in grep.text and WORD_ROOT not in grep.text


def test_rest_admin_root_view_and_zone_override(stack: Stack) -> None:
    root_list = _rest_list(stack, stack.key_admin)
    assert root_list.status_code == 200, root_list.text
    root_paths = _collect_paths(root_list.json())
    assert FILE_ROOT in root_paths and not any(
        p.endswith(FILE_A) or p.endswith(FILE_B) for p in root_paths
    )

    # ?zone=<id> gives the admin that tenant's namespaced view
    zone_list = _rest_list(stack, stack.key_admin, zone=ZONE_A)
    assert zone_list.status_code == 200, zone_list.text
    zone_paths = _collect_paths(zone_list.json())
    assert FILE_A in zone_paths, sorted(zone_paths)
    assert not any(p.endswith(FILE_B) or p.endswith(FILE_ROOT) or "/zone/" in p for p in zone_paths)


def test_admin_all_zones_is_the_anti_vacuity_control_and_is_audited(stack: Stack) -> None:
    before = len(stack.audit_events)
    paths = _rpc_list(stack, stack.key_admin, all_zones=True)
    assert FILE_ROOT in paths, sorted(paths)
    assert f"/zone/{ZONE_A}{FILE_A}" in paths, sorted(paths)
    assert f"/zone/{ZONE_B}{FILE_B}" in paths, sorted(paths)

    new_events = [e for e in stack.audit_events[before:] if e[0] == ALL_ZONES_AUDIT_EVENT]
    assert len(new_events) == 1, new_events
    payload = new_events[0][1]
    assert payload["operation"] == "list"
    assert payload["subject_id"] == "root-op"
    assert payload["zone_id"] == ROOT_ZONE_ID

    rest = _rest_list(stack, stack.key_admin, all_zones="true")
    assert rest.status_code == 200, rest.text
    assert FILE_ROOT in _collect_paths(rest.json())
