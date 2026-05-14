# Issue #3872 Remote Hub Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement full remote hub token administration over MCP for `create`, `list`, `revoke`, and `status`.

**Architecture:** Add hub-specific admin RPC handlers on the `nexusd` server so remote behavior can reuse the same multi-zone token semantics as the local hub CLI. Register one MCP tool, `nexus_hub_admin`, that delegates to those RPCs using the request bearer token. Add a synchronous streamable-HTTP MCP client for the `nexus hub` CLI when `--remote` is provided.

**Tech Stack:** Python, Click, FastMCP, SQLAlchemy, pytest, httpx streamable HTTP, existing Nexus gRPC RPC dispatch.

---

## File Structure

- Create `src/nexus/server/rpc/handlers/hub_admin.py`: server-side hub token create/list/revoke/status handlers, protected by `require_admin`.
- Modify `src/nexus/server/rpc/dispatch.py`: register hub admin RPC handlers.
- Modify `src/nexus/server/_rpc_param_overrides.py`: add typed param dataclasses for hub admin RPCs.
- Create `src/nexus/bricks/mcp/hub_admin_tool.py`: register and implement `nexus_hub_admin`.
- Modify `src/nexus/bricks/mcp/server.py`: register the new MCP tool after `_get_nexus_instance` is defined.
- Create `src/nexus/cli/commands/_hub_remote.py`: normalize MCP URLs, call `tools/call`, parse MCP results, and raise Click errors.
- Modify `src/nexus/cli/commands/hub.py`: add `--remote` and `--admin-token` to token create/list/revoke and status, dispatch to remote path, preserve local behavior.
- Modify `docs/hub-deploy.md`: document remote admin flow and remove the follow-up marker.
- Add tests:
  - `tests/unit/server/rpc/handlers/test_hub_admin.py`
  - `tests/unit/bricks/mcp/test_hub_admin_tool.py`
  - `tests/unit/cli/test_hub_remote.py`
  - update existing hub CLI tests only where shared Click signatures require it.

---

### Task 1: Hub Admin RPC Handler

**Files:**
- Create: `tests/unit/server/rpc/handlers/test_hub_admin.py`
- Create: `src/nexus/server/rpc/handlers/hub_admin.py`
- Modify: `src/nexus/server/rpc/dispatch.py`
- Modify: `src/nexus/server/_rpc_param_overrides.py`

- [ ] **Step 1: Write failing tests for server-side hub token operations**

Create `tests/unit/server/rpc/handlers/test_hub_admin.py` with tests covering:

```python
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusPermissionError
from nexus.server.rpc.handlers import hub_admin
from nexus.storage.models import APIKeyModel, APIKeyZoneModel, Base, ZoneModel


@dataclass
class Params:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def _provider(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hub_admin.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    with Session() as session, session.begin():
        for zone_id in ("root", "eng", "ops"):
            session.add(ZoneModel(zone_id=zone_id, name=zone_id, phase="Active"))
    return SimpleNamespace(session_factory=Session)


def test_create_token_writes_multi_zone_permissions(tmp_path):
    provider = _provider(tmp_path)
    result = hub_admin.handle_admin_hub_token_create(
        provider,
        Params(
            name="alice",
            zones=[
                {"zone_id": "eng", "permissions": "rw"},
                {"zone_id": "ops", "permissions": "r"},
            ],
            zones_glob=None,
            is_admin=False,
            expires=None,
            user_id=None,
        ),
        SimpleNamespace(is_admin=True),
    )
    assert result["token"].startswith("sk-")
    assert result["name"] == "alice"
    assert result["zones"] == ["eng", "ops"]
    with provider.session_factory() as session:
        rows = session.execute(
            select(APIKeyZoneModel.zone_id, APIKeyZoneModel.permissions)
        ).all()
    assert rows == [("eng", "rw"), ("ops", "r")]


def test_list_tokens_returns_local_json_shape(tmp_path):
    provider = _provider(tmp_path)
    hub_admin.handle_admin_hub_token_create(
        provider,
        Params(name="alice", zones=[{"zone_id": "eng", "permissions": "rw"}], zones_glob=None, is_admin=True, expires=None, user_id=None),
        SimpleNamespace(is_admin=True),
    )
    result = hub_admin.handle_admin_hub_token_list(
        provider,
        Params(show_revoked=False),
        SimpleNamespace(is_admin=True),
    )
    assert result["tokens"][0]["name"] == "alice"
    assert result["tokens"][0]["zones"] == ["eng"]
    assert set(result["tokens"][0]) >= {"key_id", "name", "zone", "zones", "admin", "created", "last_used", "revoked", "revoked_at"}


def test_revoke_token_accepts_name_and_sets_revoked_at(tmp_path):
    provider = _provider(tmp_path)
    hub_admin.handle_admin_hub_token_create(
        provider,
        Params(name="alice", zones=[{"zone_id": "eng", "permissions": "rw"}], zones_glob=None, is_admin=False, expires=None, user_id=None),
        SimpleNamespace(is_admin=True),
    )
    result = hub_admin.handle_admin_hub_token_revoke(
        provider,
        Params(identifier="alice"),
        SimpleNamespace(is_admin=True),
    )
    assert result["revoked"] is True
    with provider.session_factory() as session:
        row = session.scalar(select(APIKeyModel).where(APIKeyModel.name == "alice"))
    assert row is not None
    assert bool(row.revoked) is True
    assert row.revoked_at is not None


def test_non_admin_rejected(tmp_path):
    provider = _provider(tmp_path)
    with pytest.raises(NexusPermissionError):
        hub_admin.handle_admin_hub_token_list(
            provider,
            Params(show_revoked=False),
            SimpleNamespace(is_admin=False),
        )
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/unit/server/rpc/handlers/test_hub_admin.py -q
```

Expected: FAIL because `nexus.server.rpc.handlers.hub_admin` does not exist.

- [ ] **Step 3: Implement hub admin handlers**

Create `src/nexus/server/rpc/handlers/hub_admin.py` with:

```python
"""Hub admin RPC handlers for remote `nexus hub` operations (#3872)."""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from nexus.server.rpc.handlers.admin import require_admin, require_database_auth
from nexus.storage.api_key_ops import create_api_key, get_primary_zones_for_keys
from nexus.storage.models import APIKeyModel, APIKeyZoneModel, ZoneModel

_DURATION_RE = re.compile(r"^(\d+)([dhm])$")


def _parse_duration(text: str) -> timedelta:
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(f"invalid duration {text!r}: expected Nd / Nh / Nm (e.g. 90d)")
    value, unit = int(match.group(1)), match.group(2)
    return {"d": timedelta(days=value), "h": timedelta(hours=value), "m": timedelta(minutes=value)}[unit]


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else "-"


def _iso_or_none(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _zone_entries(raw: list[dict[str, Any]]) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for item in raw:
        zone_id = str(item.get("zone_id", "")).strip()
        permissions = str(item.get("permissions") or "rw").strip()
        if not zone_id:
            raise ValueError("zone_id must not be empty")
        entries.append((zone_id, permissions))
    return entries


def _resolve_zones(session: Any, params: Any) -> list[tuple[str, str]]:
    zones_glob = getattr(params, "zones_glob", None)
    raw_zones = getattr(params, "zones", None) or []
    if zones_glob and raw_zones:
        raise ValueError("zones and zones_glob are mutually exclusive")
    if zones_glob:
        active = session.execute(
            select(ZoneModel)
            .where(ZoneModel.phase == "Active")
            .where(ZoneModel.deleted_at.is_(None))
        ).scalars().all()
        matched = sorted(z.zone_id for z in active if fnmatch.fnmatch(z.zone_id, zones_glob))
        if not matched:
            known = sorted(z.zone_id for z in active)
            raise ValueError(
                f"zones_glob {zones_glob!r}: no active zones match. "
                f"Active zones: {', '.join(known) or '(none)'}."
            )
        return [(zone_id, "rw") for zone_id in matched]
    entries = _zone_entries(raw_zones)
    if not entries:
        raise ValueError("zones must contain at least one entry")
    return entries


def _ensure_bootstrap_zones(session: Any, zones: list[tuple[str, str]]) -> None:
    any_zone = session.execute(select(ZoneModel).limit(1)).scalars().first()
    if any_zone is not None:
        return
    for zone_id, _permissions in zones:
        if not session.scalar(select(ZoneModel).where(ZoneModel.zone_id == zone_id)):
            session.add(ZoneModel(zone_id=zone_id, name=zone_id, phase="Active"))
    session.flush()


def _token_zones_by_key(session: Any, key_ids: list[str]) -> dict[str, list[str]]:
    if not key_ids:
        return {}
    rows = session.execute(
        select(APIKeyZoneModel.key_id, APIKeyZoneModel.zone_id)
        .where(APIKeyZoneModel.key_id.in_(key_ids))
        .order_by(APIKeyZoneModel.granted_at.asc(), APIKeyZoneModel.zone_id.asc())
    ).all()
    zones_by_key: dict[str, list[str]] = {key_id: [] for key_id in key_ids}
    for key_id, zone_id in rows:
        zones_by_key.setdefault(key_id, []).append(zone_id)
    return zones_by_key


def _tokens_payload(session: Any, rows: list[APIKeyModel]) -> list[dict[str, Any]]:
    key_ids = [row.key_id for row in rows]
    zones_by_key = _token_zones_by_key(session, key_ids)
    primary_by_key = get_primary_zones_for_keys(session, key_ids) if key_ids else {}
    payload = []
    for row in rows:
        zones = zones_by_key.get(row.key_id, [])
        primary = primary_by_key.get(row.key_id)
        if zones and primary in zones:
            zones = [primary] + sorted(zone for zone in zones if zone != primary)
        elif not zones and primary:
            zones = [primary]
        payload.append(
            {
                "key_id": row.key_id,
                "name": row.name,
                "zone": primary,
                "zones": zones,
                "admin": bool(row.is_admin),
                "created": _iso(row.created_at),
                "last_used": _iso(row.last_used_at),
                "revoked": bool(row.revoked),
                "revoked_at": _iso(row.revoked_at),
            }
        )
    return payload


def handle_admin_hub_token_create(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    require_admin(context)
    require_database_auth(auth_provider)
    expires_at = None
    if getattr(params, "expires", None):
        expires_at = datetime.now(UTC) + _parse_duration(params.expires)
    with auth_provider.session_factory() as session, session.begin():
        existing = session.execute(
            select(APIKeyModel)
            .where(APIKeyModel.name == params.name)
            .where(APIKeyModel.revoked == 0)
        ).scalars().first()
        if existing is not None:
            raise ValueError(
                f"token named {params.name!r} already exists (key_id={existing.key_id})"
            )
        zones = _resolve_zones(session, params)
        _ensure_bootstrap_zones(session, zones)
        key_id, raw_key = create_api_key(
            session,
            user_id=getattr(params, "user_id", None) or params.name,
            name=params.name,
            zones=zones,
            is_admin=bool(getattr(params, "is_admin", False)),
            expires_at=expires_at,
        )
    return {
        "key_id": key_id,
        "token": raw_key,
        "name": params.name,
        "zones": [zone_id for zone_id, _permissions in zones],
        "admin": bool(getattr(params, "is_admin", False)),
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def handle_admin_hub_token_list(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    require_admin(context)
    require_database_auth(auth_provider)
    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).order_by(APIKeyModel.created_at.desc())
        if not getattr(params, "show_revoked", False):
            stmt = stmt.where(APIKeyModel.revoked == 0)
        rows = list(session.execute(stmt).scalars().all())
        return {"tokens": _tokens_payload(session, rows)}


def handle_admin_hub_token_revoke(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    require_admin(context)
    require_database_auth(auth_provider)
    identifier = params.identifier
    with auth_provider.session_factory() as session, session.begin():
        matches = list(
            session.execute(
                select(APIKeyModel)
                .where(APIKeyModel.revoked == 0)
                .where(
                    (APIKeyModel.key_id == identifier)
                    | (APIKeyModel.key_id.startswith(identifier))
                    | (APIKeyModel.name == identifier)
                )
            ).scalars().all()
        )
        if not matches:
            raise FileNotFoundError(f"no active token matches {identifier!r}")
        if len(matches) > 1:
            names = ", ".join(f"{match.name} ({match.key_id})" for match in matches)
            raise ValueError(f"ambiguous: {len(matches)} tokens match {identifier!r} - {names}")
        row = matches[0]
        row.revoked = 1
        row.revoked_at = datetime.now(UTC)
        return {"key_id": row.key_id, "name": row.name, "revoked": True}
```

Also add the status helpers in the same file by reusing the local status shapes:

```python
def _read_redis_stats() -> dict[str, Any]:
    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    if not url:
        return {"qps_5m": None, "connections": None, "redis": "n/a"}
    try:
        import redis
    except ImportError:
        return {"qps_5m": None, "connections": None, "redis": "n/a"}
    try:
        client = redis.from_url(url, socket_timeout=2)
        client.ping()
        now_min = int(time.time()) // 60
        total = sum(int(v) for v in client.mget([f"nexus:hub:qps:{now_min - i}" for i in range(5)]) if v is not None)
        active = client.scard(f"nexus:hub:active:{now_min}")
        return {"qps_5m": round(total / 300.0, 2), "connections": int(active), "redis": "ok"}
    except Exception:
        return {"qps_5m": None, "connections": None, "redis": "n/a"}


def handle_admin_hub_status(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    require_admin(context)
    require_database_auth(auth_provider)
    endpoint = getattr(params, "endpoint", None) or "remote"
    profile = os.environ.get("NEXUS_PROFILE", "full")
    with auth_provider.session_factory() as session:
        active = session.execute(select(func.count()).select_from(APIKeyModel).where(APIKeyModel.revoked == 0)).scalar() or 0
        revoked = session.execute(select(func.count()).select_from(APIKeyModel).where(APIKeyModel.revoked == 1)).scalar() or 0
        payload = {
            "endpoint": endpoint,
            "profile": profile,
            "postgres": "ok",
            "tokens": {"active": int(active), "revoked": int(revoked)},
            **_read_redis_stats(),
        }
        if getattr(params, "detail", False):
            zone_ids = [
                str(zone_id)
                for zone_id in session.execute(
                    select(ZoneModel.zone_id)
                    .where(ZoneModel.phase == "Active")
                    .where(ZoneModel.deleted_at.is_(None))
                    .order_by(ZoneModel.zone_id.asc())
                ).scalars().all()
            ]
            rows = list(session.execute(select(APIKeyModel).order_by(APIKeyModel.created_at.desc())).scalars().all())
            payload.update(
                {
                    "detail": True,
                    "zones": [{"zone_id": zone_id, "clients": None, "qps_5m": None} for zone_id in zone_ids],
                    "tokens_detail": [
                        {
                            "key_id": token["key_id"],
                            "name": token["name"],
                            "zones": token["zones"],
                            "admin": token["admin"],
                            "created": token["created"] if token["created"] != "-" else None,
                            "last_seen": token["last_used"] if token["last_used"] != "-" else None,
                            "revoked": token["revoked"],
                            "revoked_at": token["revoked_at"] if token["revoked_at"] != "-" else None,
                        }
                        for token in _tokens_payload(session, rows)
                    ],
                    "rate_limits": {"window_seconds": 300, "hits_by_tier": {"anonymous": None, "authenticated": None, "premium": None}},
                    "search": {"zones": [{"zone_id": zone_id, "zoekt_index_size_bytes": None, "zoekt_index_size_display": None, "zoekt_last_indexed": None, "txtai_queue_depth": None, "last_indexed": None} for zone_id in zone_ids]},
                }
            )
        return payload
```

- [ ] **Step 4: Register RPC params and dispatch entries**

In `src/nexus/server/_rpc_param_overrides.py`, add dataclasses:

```python
@dataclass
class AdminHubTokenCreateParams:
    name: str
    zones: list[dict[str, Any]] = field(default_factory=list)
    zones_glob: str | None = None
    is_admin: bool = False
    expires: str | None = None
    user_id: str | None = None


@dataclass
class AdminHubTokenListParams:
    show_revoked: bool = False


@dataclass
class AdminHubTokenRevokeParams:
    identifier: str


@dataclass
class AdminHubStatusParams:
    endpoint: str | None = None
    detail: bool = False
```

Add to `OVERRIDE_METHOD_PARAMS`:

```python
"admin_hub_token_create": AdminHubTokenCreateParams,
"admin_hub_token_list": AdminHubTokenListParams,
"admin_hub_token_revoke": AdminHubTokenRevokeParams,
"admin_hub_status": AdminHubStatusParams,
```

In `src/nexus/server/rpc/dispatch.py`, import the four handlers and add dispatch entries with `pass_auth_provider=True`.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
uv run pytest tests/unit/server/rpc/handlers/test_hub_admin.py -q
```

Expected: PASS.

---

### Task 2: MCP Hub Admin Tool

**Files:**
- Create: `tests/unit/bricks/mcp/test_hub_admin_tool.py`
- Create: `src/nexus/bricks/mcp/hub_admin_tool.py`
- Modify: `src/nexus/bricks/mcp/server.py`
- Modify: `src/nexus/config/tool_profiles.yaml`

- [ ] **Step 1: Write failing MCP tool tests**

Create `tests/unit/bricks/mcp/test_hub_admin_tool.py`:

```python
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nexus.bricks.mcp.server import create_mcp_server
from tests.unit.bricks.mcp.test_mcp_server_tools import get_tool, tool_exists


@pytest.mark.asyncio
async def test_nexus_hub_admin_tool_registered():
    server = await create_mcp_server(nx=Mock())
    assert tool_exists(server, "nexus_hub_admin")


@pytest.mark.asyncio
async def test_nexus_hub_admin_list_delegates_to_remote_service():
    service = Mock()
    service.admin_hub_token_list.return_value = {"tokens": []}
    nx = Mock()
    nx.service.return_value = service
    server = await create_mcp_server(nx=nx)
    tool = get_tool(server, "nexus_hub_admin")
    result = await tool.fn(action="list_tokens", arguments={"show_revoked": True})
    assert json.loads(result) == {"tokens": []}
    service.admin_hub_token_list.assert_called_once_with(show_revoked=True)


@pytest.mark.asyncio
async def test_nexus_hub_admin_permission_error_has_403_status():
    from nexus.contracts.exceptions import NexusPermissionError

    service = Mock()
    service.admin_hub_token_list.side_effect = NexusPermissionError("Admin privileges required")
    nx = Mock()
    nx.service.return_value = service
    server = await create_mcp_server(nx=nx)
    tool = get_tool(server, "nexus_hub_admin")
    result = await tool.fn(action="list_tokens", arguments={})
    payload = json.loads(result)
    assert payload["error"]["status"] == 403
    assert "Admin privileges required" in payload["error"]["message"]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/unit/bricks/mcp/test_hub_admin_tool.py -q
```

Expected: FAIL because `nexus_hub_admin` is not registered.

- [ ] **Step 3: Implement MCP registration helper**

Create `src/nexus/bricks/mcp/hub_admin_tool.py`:

```python
"""MCP hub-admin tool registration (#3872)."""

from __future__ import annotations

import json
from typing import Any, Callable

from fastmcp import Context

from nexus.contracts.exceptions import NexusPermissionError


def _admin_service(nx_instance: Any) -> Any:
    if not hasattr(nx_instance, "service"):
        raise RuntimeError("Nexus service registry is unavailable")
    service = nx_instance.service("mcp")
    if service is None:
        raise RuntimeError("Remote admin service is unavailable")
    return service


def _json_error(status: int, message: str) -> str:
    return json.dumps({"error": {"status": status, "message": message}}, indent=2)


def register_hub_admin_tool(mcp: Any, get_nexus_instance: Callable[[Context | None], Any]) -> None:
    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    async def nexus_hub_admin(
        action: str,
        arguments: dict[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Administer Nexus hub tokens. Requires an admin bearer token."""
        args = arguments or {}
        nx_instance = get_nexus_instance(ctx)
        service = _admin_service(nx_instance)
        try:
            if action == "create_token":
                result = service.admin_hub_token_create(**args)
            elif action == "list_tokens":
                result = service.admin_hub_token_list(**args)
            elif action == "revoke_token":
                result = service.admin_hub_token_revoke(**args)
            elif action == "status":
                result = service.admin_hub_status(**args)
            else:
                return _json_error(400, f"unknown hub admin action: {action}")
            return json.dumps(result, indent=2, default=str)
        except NexusPermissionError as exc:
            return _json_error(403, str(exc))
        except FileNotFoundError as exc:
            return _json_error(404, str(exc))
        except ValueError as exc:
            return _json_error(400, str(exc))
```

In `src/nexus/bricks/mcp/server.py`, import and call after `mcp = FastMCP(name)` and after `_get_nexus_instance` exists:

```python
from nexus.bricks.mcp.hub_admin_tool import register_hub_admin_tool

register_hub_admin_tool(mcp, _get_nexus_instance)
```

In `src/nexus/config/tool_profiles.yaml`, add `nexus_hub_admin` to the `full` profile tools list.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/unit/bricks/mcp/test_hub_admin_tool.py -q
```

Expected: PASS.

---

### Task 3: Remote Hub CLI Client

**Files:**
- Create: `tests/unit/cli/test_hub_remote.py`
- Create: `src/nexus/cli/commands/_hub_remote.py`

- [ ] **Step 1: Write failing helper tests**

Create `tests/unit/cli/test_hub_remote.py` with helper-level tests:

```python
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from click import ClickException

from nexus.cli.commands import _hub_remote


def test_normalize_remote_url_appends_mcp_path():
    assert _hub_remote.normalize_mcp_url("https://nexus.example.com") == "https://nexus.example.com/mcp"
    assert _hub_remote.normalize_mcp_url("https://nexus.example.com/") == "https://nexus.example.com/mcp"
    assert _hub_remote.normalize_mcp_url("https://nexus.example.com/mcp") == "https://nexus.example.com/mcp"


def test_extract_tool_payload_parses_text_json():
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"content": [{"type": "text", "text": json.dumps({"tokens": []})}]},
    }
    assert _hub_remote.extract_tool_payload(payload) == {"tokens": []}


def test_extract_tool_payload_raises_for_403_error():
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"error": {"status": 403, "message": "Admin privileges required"}})}
            ]
        },
    }
    with pytest.raises(ClickException, match="Admin privileges required"):
        _hub_remote.extract_tool_payload(payload)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/unit/cli/test_hub_remote.py -q
```

Expected: FAIL because `_hub_remote` does not exist.

- [ ] **Step 3: Implement remote MCP client helper**

Create `src/nexus/cli/commands/_hub_remote.py`:

```python
"""Remote MCP client helpers for `nexus hub` (#3872)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse, urlunparse

import click
import httpx


def normalize_mcp_url(remote: str) -> str:
    parsed = urlparse(remote)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise click.ClickException(f"invalid --remote URL: {remote!r}")
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        path = "/mcp"
    return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))


def extract_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "error" in payload:
        raise click.ClickException(str(payload["error"].get("message", payload["error"])))
    result = payload.get("result") or {}
    content = result.get("content") or []
    text = ""
    if content and isinstance(content[0], dict):
        text = str(content[0].get("text", ""))
    if not text:
        raise click.ClickException("remote hub admin returned an empty MCP response")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"remote hub admin returned invalid JSON: {text[:200]}") from exc
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise click.ClickException(str(err.get("message", err)))
    if not isinstance(data, dict):
        raise click.ClickException("remote hub admin returned a non-object response")
    return data


def call_hub_admin(
    remote: str,
    admin_token: str,
    action: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    url = normalize_mcp_url(remote)
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "nexus-hub-cli", "version": "1"},
        },
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, headers=headers, json=init_body) as resp:
                if resp.status_code in (401, 403):
                    raise click.ClickException(f"remote hub admin rejected credentials ({resp.status_code})")
                resp.raise_for_status()
                session_id = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
                for _line in resp.iter_lines():
                    pass
            if not session_id:
                raise click.ClickException("remote MCP endpoint did not return mcp-session-id")
            session_headers = {**headers, "Mcp-Session-Id": session_id}
            client.post(
                url,
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            body = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "nexus_hub_admin",
                    "arguments": {"action": action, "arguments": arguments or {}},
                },
            }
            with client.stream("POST", url, headers=session_headers, json=body) as resp:
                if resp.status_code in (401, 403):
                    raise click.ClickException(f"remote hub admin rejected credentials ({resp.status_code})")
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        return extract_tool_payload(json.loads(line[6:]))
    except httpx.HTTPError as exc:
        raise click.ClickException(f"remote hub admin request failed for {url}: {exc}") from exc
    raise click.ClickException("remote hub admin returned no MCP data event")
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/unit/cli/test_hub_remote.py -q
```

Expected: PASS.

---

### Task 4: Wire Remote Flags Into Hub CLI

**Files:**
- Modify: `src/nexus/cli/commands/hub.py`
- Modify: `tests/unit/cli/test_hub_remote.py`

- [ ] **Step 1: Write failing CLI tests**

Extend `tests/unit/cli/test_hub_remote.py`:

```python
from click.testing import CliRunner
from nexus.cli.commands.hub import hub


def test_remote_token_list_uses_mcp_client(monkeypatch):
    seen = {}
    def fake_call(remote, admin_token, action, arguments=None):
        seen.update({"remote": remote, "admin_token": admin_token, "action": action, "arguments": arguments})
        return {"tokens": [{"key_id": "kid_1234567890", "name": "alice", "zone": "eng", "zones": ["eng"], "admin": False, "created": "-", "last_used": "-", "revoked": False, "revoked_at": "-"}]}
    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin", fake_call)
    result = CliRunner().invoke(hub, ["token", "list", "--remote", "https://nexus.example.com", "--admin-token", "sk-admin", "--json"])
    assert result.exit_code == 0, result.output
    assert seen["action"] == "list_tokens"
    assert seen["arguments"] == {"show_revoked": False}
    assert json.loads(result.output)["tokens"][0]["name"] == "alice"


def test_remote_token_create_sends_zones_and_prints_token(monkeypatch):
    seen = {}
    def fake_call(remote, admin_token, action, arguments=None):
        seen.update({"action": action, "arguments": arguments})
        return {"key_id": "kid", "token": "sk-created", "name": "alice", "zones": ["eng"], "admin": True, "expires_at": None}
    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin", fake_call)
    result = CliRunner().invoke(hub, ["token", "create", "--name", "alice", "--zones", "eng:rwx", "--admin", "--remote", "https://nexus.example.com", "--admin-token", "sk-admin"])
    assert result.exit_code == 0, result.output
    assert seen["action"] == "create_token"
    assert seen["arguments"]["zones"] == [{"zone_id": "eng", "permissions": "rwx"}]
    assert "sk-created" in result.output


def test_remote_requires_admin_token(monkeypatch):
    result = CliRunner().invoke(hub, ["token", "list", "--remote", "https://nexus.example.com"])
    assert result.exit_code == 1
    assert "--admin-token" in result.output
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/unit/cli/test_hub_remote.py -q
```

Expected: FAIL because `hub.py` has no remote options.

- [ ] **Step 3: Add remote option helpers and dispatch**

In `src/nexus/cli/commands/hub.py`:

```python
from nexus.cli.commands._hub_remote import call_hub_admin, normalize_mcp_url
```

Add helpers near `_parse_zones_csv`:

```python
def _remote_options(func: Any) -> Any:
    func = click.option("--admin-token", envvar="NEXUS_HUB_ADMIN_TOKEN", help="Admin bearer token for --remote.")(func)
    func = click.option("--remote", help="Remote MCP hub URL. Host URLs are normalized to /mcp.")(func)
    return func


def _require_remote_token(remote: str | None, admin_token: str | None) -> None:
    if remote and not admin_token:
        raise click.ClickException("--admin-token is required when --remote is used")


def _zones_for_remote(zones: list[str | tuple[str, str]]) -> list[dict[str, str]]:
    out = []
    for entry in zones:
        if isinstance(entry, tuple):
            out.append({"zone_id": entry[0], "permissions": entry[1]})
        else:
            out.append({"zone_id": entry, "permissions": "rw"})
    return out
```

Apply `@_remote_options` to `token_create`, `token_list`, `token_revoke`, and `hub_status`. Add `remote` and `admin_token` parameters to each function.

At the start of `token_create`, after mutual-exclusion validation and duration validation:

```python
if remote:
    _require_remote_token(remote, admin_token)
    if zones_glob is not None:
        remote_zones = []
    elif zones_csv is not None:
        remote_zones = _zones_for_remote(_parse_zones_csv(zones_csv))
    else:
        remote_zones = _zones_for_remote([z.strip() for z in zone_alias.split(",") if z.strip()] if zone_alias else [])
    result = call_hub_admin(
        remote,
        admin_token or "",
        "create_token",
        {
            "name": name,
            "zones": remote_zones,
            "zones_glob": zones_glob,
            "is_admin": is_admin,
            "expires": expires,
            "user_id": user_id,
        },
    )
    click.echo(f"key_id: {result['key_id']}")
    click.echo(f"token:  {result['token']}")
    click.echo("")
    click.echo("Save this token now - it will not be shown again.")
    return
```

At the start of `token_list`:

```python
if remote:
    _require_remote_token(remote, admin_token)
    payload = call_hub_admin(remote, admin_token or "", "list_tokens", {"show_revoked": show_revoked})
    _emit_token_list_payload(payload, as_json=as_json)
    return
```

Extract the existing local token list rendering into `_emit_token_list_payload(payload, as_json)` so remote and local share output.

At the start of `token_revoke`:

```python
if remote:
    _require_remote_token(remote, admin_token)
    result = call_hub_admin(remote, admin_token or "", "revoke_token", {"identifier": identifier})
    click.echo(f"revoked {result['name']} ({result['key_id']}). Effective within 60s (auth cache TTL).")
    return
```

At the start of `hub_status`:

```python
if remote:
    _require_remote_token(remote, admin_token)
    payload = call_hub_admin(
        remote,
        admin_token or "",
        "status",
        {"detail": detail, "endpoint": normalize_mcp_url(remote)},
    )
    if as_json:
        click.echo(_json.dumps(payload, indent=2))
    else:
        _emit_base_status_text(payload)
        if detail:
            _emit_detail_status_text(payload)
    if payload.get("postgres") != "ok":
        raise SystemExit(2)
    return
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/unit/cli/test_hub_remote.py tests/unit/cli/test_hub.py tests/unit/cli/test_hub_token_list_primary_alias.py -q
```

Expected: PASS.

---

### Task 5: Documentation and Integration Tests

**Files:**
- Modify: `docs/hub-deploy.md`
- Create or modify: `tests/e2e/self_contained/cli/test_hub_flow.py`

- [ ] **Step 1: Write or extend skipped e2e coverage**

Add an e2e test guarded by existing environment variables:

```python
def test_hub_remote_token_list_over_mcp(hub_cli_env: dict[str, str]) -> None:
    admin_token = _require("NEXUS_ADMIN_KEY")
    mcp_base_url = _mcp_url()
    result = _run(
        ["hub", "token", "list", "--remote", mcp_base_url, "--admin-token", admin_token, "--json"],
        env={k: v for k, v in hub_cli_env.items() if k != "NEXUS_DATABASE_URL"},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "tokens" in payload
```

This test must skip cleanly when a live hub stack is absent.

- [ ] **Step 2: Update docs**

In `docs/hub-deploy.md`:

- Add a subsection under token lifecycle:

```markdown
Remote administration:

1. Create the bootstrap admin token locally on the hub host:
   `nexus hub token create --name root --admin --zone root`.
2. From a workstation, point at the MCP endpoint:
   `nexus hub token list --remote https://nexus.example.com --admin-token sk-...`.
3. `NEXUS_HUB_ADMIN_TOKEN` may be used instead of repeating `--admin-token`.
```

- Replace the "Remote admin CLI" follow-up bullet with implemented wording.

- [ ] **Step 3: Run focused tests**

Run:

```bash
uv run pytest tests/unit/server/rpc/handlers/test_hub_admin.py tests/unit/bricks/mcp/test_hub_admin_tool.py tests/unit/cli/test_hub_remote.py tests/unit/cli/test_hub.py tests/unit/cli/test_hub_token_list_primary_alias.py -q
```

Expected: PASS.

- [ ] **Step 4: Run lint/format on touched Python files**

Run:

```bash
uv run ruff check src/nexus/server/rpc/handlers/hub_admin.py src/nexus/bricks/mcp/hub_admin_tool.py src/nexus/bricks/mcp/server.py src/nexus/cli/commands/_hub_remote.py src/nexus/cli/commands/hub.py tests/unit/server/rpc/handlers/test_hub_admin.py tests/unit/bricks/mcp/test_hub_admin_tool.py tests/unit/cli/test_hub_remote.py
uv run ruff format --check src/nexus/server/rpc/handlers/hub_admin.py src/nexus/bricks/mcp/hub_admin_tool.py src/nexus/bricks/mcp/server.py src/nexus/cli/commands/_hub_remote.py src/nexus/cli/commands/hub.py tests/unit/server/rpc/handlers/test_hub_admin.py tests/unit/bricks/mcp/test_hub_admin_tool.py tests/unit/cli/test_hub_remote.py
```

Expected: PASS. If formatting fails, run `uv run ruff format ...` and rerun the check.

---

## Plan Self-Review

Spec coverage:

- Remote `create`, `list`, `revoke`, and `status` are covered by Tasks 1, 3, and 4.
- MCP-side admin tool is covered by Task 2.
- Non-admin rejection is covered by Tasks 1 and 2.
- Local behavior preservation is covered by rerunning existing hub CLI tests.
- Documentation is covered by Task 5.

Deferred-work scan:

- No unresolved markers or deferred implementation notes remain.

Type consistency:

- RPC method names use the `admin_hub_*` prefix throughout params, dispatch, MCP tool calls, and CLI remote calls.
- CLI action names use `create_token`, `list_tokens`, `revoke_token`, and `status` throughout.
