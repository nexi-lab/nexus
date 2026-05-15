# Issue 3872 Remote Hub Admin CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--remote` workstation support for `nexus hub token create/list/revoke` and `nexus hub status` through admin-only MCP tools.

**Architecture:** The public workstation path is CLI -> MCP HTTP `/mcp` -> MCP admin tool -> hub backend RPC -> DB-backed shared hub operations. The extra backend RPC hop is required because the supported hub runtime runs the MCP frontend as a remote client of the RPC service; direct database access from the MCP frontend is not available in that deployment.

**Tech Stack:** Python, Click, FastMCP, httpx, SQLAlchemy, existing Nexus gRPC/RPC dispatch, pytest.

---

## File Map

- Create `src/nexus/hub/__init__.py`: package marker for shared hub admin code.
- Create `src/nexus/hub/admin_ops.py`: DB-backed token/status operations shared by local CLI and backend RPC handlers.
- Create `src/nexus/cli/commands/_hub_remote.py`: synchronous MCP HTTP client for remote hub admin tool calls.
- Create `src/nexus/server/rpc/handlers/hub_admin.py`: backend RPC handlers that require admin context and call `admin_ops`.
- Modify `src/nexus/cli/commands/hub.py`: add `--remote` and `--admin-token`, delegate local DB work to `admin_ops`, render remote payloads with existing output shapes.
- Modify `src/nexus/bricks/mcp/server.py`: register `nexus_hub_token_create`, `nexus_hub_token_list`, `nexus_hub_token_revoke`, and `nexus_hub_status` tools.
- Modify `src/nexus/server/_rpc_param_overrides.py`: add params for the new `hub_admin_*` backend RPC methods.
- Modify `src/nexus/server/rpc/dispatch.py`: add dispatch table entries for the new backend RPC methods.
- Modify `src/nexus/__init__.py`: expose the remote profile transport call as `nfs._nexus_remote_call_rpc` so MCP tools can forward admin RPCs through the per-request remote `NexusFS`.
- Test `tests/unit/hub/test_admin_ops.py`: shared operation behavior.
- Test `tests/unit/cli/test_hub_remote.py`: remote CLI flags, URL normalization, tool routing, and output rendering.
- Test `tests/unit/server/rpc/handlers/test_hub_admin.py`: backend RPC admin enforcement and operation delegation.
- Test `tests/unit/bricks/mcp/test_hub_admin_tools.py`: MCP tool registration, non-admin rejection, and remote RPC forwarding.
- Update existing hub CLI tests only where import paths change because behavior moved into `admin_ops`.

## Implementation Note From Code Inspection

The approved design described MCP tools resolving a database auth provider directly. The repo now rejects embedded DB-backed HTTP MCP mode in `src/nexus/cli/commands/mcp.py`, and `nexus up` runs a two-service hub. To make the accepted `nexus up --build` verification meaningful, implement MCP tools as public admin tools that forward to new backend `hub_admin_*` RPC handlers. Those handlers run beside the database auth provider and enforce `is_admin` through the existing RPC context.

## Task 1: Shared Hub Operation Tests

**Files:**
- Create: `tests/unit/hub/test_admin_ops.py`
- Later create: `src/nexus/hub/admin_ops.py`

- [ ] **Step 1: Write failing tests for shared create/list/revoke/status operations**

Add this test file:

```python
from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.hub import admin_ops
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        for zone_id in ("eng", "ops"):
            session.add(ZoneModel(zone_id=zone_id, name=zone_id, phase="Active"))
        session.commit()

    @contextmanager
    def _factory():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    return _factory


def test_create_hub_token_supports_multi_zone_permissions(session_factory):
    result = admin_ops.create_hub_token(
        session_factory,
        name="remote-admin",
        zones_csv="eng:r,ops:rw",
        zones_glob=None,
        is_admin=True,
        expires=None,
        user_id=None,
    )

    assert result["key_id"].startswith("nk_")
    assert result["token"].startswith("sk_")
    assert result["name"] == "remote-admin"
    assert result["admin"] is True
    assert result["zones"] == [{"zone_id": "eng", "permission": "r"}, {"zone_id": "ops", "permission": "rw"}]


def test_list_hub_tokens_returns_local_cli_payload_shape(session_factory):
    with session_factory() as session:
        key_id, _ = create_api_key(
            session,
            user_id="alice",
            name="alice-token",
            zones=["eng", ("ops", "rw")],
            is_admin=False,
        )
        session.commit()

    payload = admin_ops.list_hub_tokens(session_factory, show_revoked=False)

    assert payload["tokens"][0]["key_id"] == key_id
    assert payload["tokens"][0]["name"] == "alice-token"
    assert payload["tokens"][0]["zone"] == "eng"
    assert payload["tokens"][0]["zones"] == [
        {"zone_id": "eng", "permission": "rw"},
        {"zone_id": "ops", "permission": "rw"},
    ]


def test_revoke_hub_token_matches_local_message(session_factory):
    with session_factory() as session:
        key_id, _ = create_api_key(session, user_id="alice", name="revoke-me", zones=["eng"])
        session.commit()

    result = admin_ops.revoke_hub_token(session_factory, identifier=key_id[:12])

    assert result["key_id"] == key_id
    assert result["name"] == "revoke-me"
    assert result["message"] == f"revoked revoke-me ({key_id}). Effective within 60s (auth cache TTL)."


def test_get_hub_status_reports_postgres_and_token_counts(session_factory, monkeypatch):
    monkeypatch.setenv("NEXUS_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("NEXUS_MCP_PORT", "8081")

    with session_factory() as session:
        create_api_key(session, user_id="admin", name="admin", zones=[], is_admin=True)
        session.commit()

    payload = admin_ops.get_hub_status(session_factory, redis_stats=lambda: {"status": "n/a"})

    assert payload["postgres"] == "ok"
    assert payload["tokens"] == {"active": 1, "revoked": 0}
    assert payload["endpoint"] == "http://127.0.0.1:8081/mcp"
```

- [ ] **Step 2: Run the tests and verify they fail because `nexus.hub` does not exist**

Run:

```bash
pytest tests/unit/hub/test_admin_ops.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'nexus.hub'`.

## Task 2: Shared Hub Operations Implementation

**Files:**
- Create: `src/nexus/hub/__init__.py`
- Create: `src/nexus/hub/admin_ops.py`
- Modify: `src/nexus/cli/commands/hub.py`

- [ ] **Step 1: Add the shared hub package and operations**

Create `src/nexus/hub/__init__.py`:

```python
"""Shared hub administration helpers."""
```

Create `src/nexus/hub/admin_ops.py` with these public interfaces and move the matching complete logic from `src/nexus/cli/commands/hub.py`:

```python
class HubAdminError(Exception):
    """Raised for user-facing hub admin failures."""


class HubAdminAmbiguousTargetError(HubAdminError):
    """Raised when a revoke identifier matches multiple active keys."""

    def __init__(self, matches: list[tuple[str, str]]) -> None:
        self.matches = matches
        super().__init__("ambiguous token identifier")


def parse_zones_csv(raw: str | None) -> list[str | tuple[str, str]]:
    """Parse zone CSV values accepted by `nexus hub token create --zones`."""


def parse_expires_at(raw: str | None) -> datetime | None:
    """Convert CLI duration text into an absolute expiry timestamp."""


def create_hub_token(
    session_factory: Callable[[], ContextManager[Any]],
    *,
    name: str,
    zones_csv: str | None,
    zones_glob: str | None,
    is_admin: bool,
    expires: str | None,
    user_id: str | None,
) -> dict[str, Any]:
    """Create a hub token and return the one-time token payload."""


def list_hub_tokens(
    session_factory: Callable[[], ContextManager[Any]],
    *,
    show_revoked: bool,
) -> dict[str, Any]:
    """Return the token list payload consumed by local and remote CLI output."""


def revoke_hub_token(
    session_factory: Callable[[], ContextManager[Any]],
    *,
    identifier: str,
) -> dict[str, Any]:
    """Revoke a token by exact key id, key id prefix, or name."""


def get_hub_status(
    session_factory: Callable[[], ContextManager[Any]],
    *,
    redis_stats: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the hub status payload consumed by local and remote CLI output."""
```

Move the current local logic from `src/nexus/cli/commands/hub.py` into those functions without changing payload keys. Use `HubAdminError` instead of `click.ClickException`; keep `HubAdminAmbiguousTargetError.matches` so Click can print ambiguous matches before exiting 2.

- [ ] **Step 2: Update local CLI paths to call `admin_ops`**

In `src/nexus/cli/commands/hub.py`:

```python
from nexus.hub.admin_ops import (
    HubAdminAmbiguousTargetError,
    HubAdminError,
    create_hub_token,
    get_hub_status,
    list_hub_tokens,
    revoke_hub_token,
)
```

Use the shared helpers in `token_create`, `token_list`, `token_revoke`, and `hub_status`. Convert `HubAdminError` to `click.ClickException`. Convert `HubAdminAmbiguousTargetError` by echoing the exact ambiguous rows to stderr and raising `SystemExit(2)`.

- [ ] **Step 3: Verify shared operation and existing hub tests pass**

Run:

```bash
pytest tests/unit/hub/test_admin_ops.py tests/unit/cli/test_hub.py tests/unit/cli/test_hub_token_list_primary_alias.py -q
```

Expected: all selected tests pass.

## Task 3: Remote CLI Client Tests

**Files:**
- Create: `tests/unit/cli/test_hub_remote.py`
- Later create: `src/nexus/cli/commands/_hub_remote.py`
- Later modify: `src/nexus/cli/commands/hub.py`

- [ ] **Step 1: Write failing remote CLI tests**

Add `tests/unit/cli/test_hub_remote.py`:

```python
from __future__ import annotations

import json

from click.testing import CliRunner

from nexus.cli.main import cli
from nexus.cli.commands import _hub_remote


def test_normalize_remote_url_appends_mcp_path():
    assert _hub_remote.normalize_mcp_url("https://nexus.example.com") == "https://nexus.example.com/mcp"
    assert _hub_remote.normalize_mcp_url("https://nexus.example.com/mcp") == "https://nexus.example.com/mcp"
    assert _hub_remote.normalize_mcp_url("https://nexus.example.com/") == "https://nexus.example.com/mcp"


def test_remote_list_requires_admin_token(monkeypatch):
    monkeypatch.delenv("NEXUS_HUB_ADMIN_TOKEN", raising=False)

    result = CliRunner().invoke(cli, ["hub", "token", "list", "--remote", "https://hub.example"])

    assert result.exit_code != 0
    assert "--admin-token or NEXUS_HUB_ADMIN_TOKEN is required with --remote" in result.output


def test_remote_list_uses_env_token_and_renders_json(monkeypatch):
    calls = []

    def fake_call(remote, token, tool_name, arguments):
        calls.append((remote, token, tool_name, arguments))
        return {
            "tokens": [
                {
                    "key_id": "nk_123",
                    "name": "admin",
                    "zone": None,
                    "zones": [],
                    "admin": True,
                    "created": "2026-05-04T12:00:00",
                    "last_used": None,
                    "revoked": False,
                    "revoked_at": None,
                }
            ]
        }

    monkeypatch.setenv("NEXUS_HUB_ADMIN_TOKEN", "sk_env")
    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin_tool", fake_call)

    result = CliRunner().invoke(
        cli,
        ["hub", "token", "list", "--remote", "https://hub.example", "--json"],
    )

    assert result.exit_code == 0
    assert calls == [("https://hub.example", "sk_env", "nexus_hub_token_list", {"show_revoked": False})]
    assert json.loads(result.output)["tokens"][0]["key_id"] == "nk_123"


def test_remote_create_calls_mcp_tool_and_prints_one_time_token(monkeypatch):
    calls = []

    def fake_call(remote, token, tool_name, arguments):
        calls.append((remote, token, tool_name, arguments))
        return {"key_id": "nk_new", "token": "sk_new", "name": "ci", "admin": False, "zones": [{"zone_id": "eng", "permission": "rw"}]}

    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin_tool", fake_call)

    result = CliRunner().invoke(
        cli,
        [
            "hub",
            "token",
            "create",
            "--name",
            "ci",
            "--zones",
            "eng:rw",
            "--remote",
            "https://hub.example/mcp",
            "--admin-token",
            "sk_admin",
        ],
    )

    assert result.exit_code == 0
    assert calls[0] == (
        "https://hub.example/mcp",
        "sk_admin",
        "nexus_hub_token_create",
        {"name": "ci", "zones": "eng:rw", "zones_glob": None, "admin": False, "expires": None, "user_id": None},
    )
    assert "key_id: nk_new" in result.output
    assert "token:  sk_new" in result.output


def test_remote_revoke_calls_mcp_tool(monkeypatch):
    calls = []

    def fake_call(remote, token, tool_name, arguments):
        calls.append((remote, token, tool_name, arguments))
        return {"key_id": "nk_old", "name": "old", "message": "revoked old (nk_old). Effective within 60s (auth cache TTL)."}

    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin_tool", fake_call)

    result = CliRunner().invoke(
        cli,
        ["hub", "token", "revoke", "old", "--remote", "https://hub.example", "--admin-token", "sk_admin"],
    )

    assert result.exit_code == 0
    assert calls == [("https://hub.example", "sk_admin", "nexus_hub_token_revoke", {"identifier": "old"})]
    assert "revoked old (nk_old)" in result.output


def test_remote_status_calls_mcp_tool_and_renders_json(monkeypatch):
    calls = []

    def fake_call(remote, token, tool_name, arguments):
        calls.append((remote, token, tool_name, arguments))
        return {"endpoint": "https://hub.example/mcp", "profile": "full", "postgres": "ok", "redis": "n/a", "tokens": {"active": 1, "revoked": 0}, "connections": None, "qps_5m": None}

    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin_tool", fake_call)

    result = CliRunner().invoke(
        cli,
        ["hub", "status", "--remote", "https://hub.example", "--admin-token", "sk_admin", "--json"],
    )

    assert result.exit_code == 0
    assert calls == [("https://hub.example", "sk_admin", "nexus_hub_status", {})]
    assert json.loads(result.output)["postgres"] == "ok"
```

- [ ] **Step 2: Run the tests and verify they fail on missing `_hub_remote` or missing options**

Run:

```bash
pytest tests/unit/cli/test_hub_remote.py -q
```

Expected: failure from missing `nexus.cli.commands._hub_remote` or missing `--remote` options.

## Task 4: Remote CLI Client Implementation

**Files:**
- Create: `src/nexus/cli/commands/_hub_remote.py`
- Modify: `src/nexus/cli/commands/hub.py`

- [ ] **Step 1: Implement MCP HTTP helper**

Create `src/nexus/cli/commands/_hub_remote.py` with:

```python
from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx


class HubRemoteError(Exception):
    pass


def normalize_mcp_url(remote: str) -> str:
    parsed = urlparse(remote)
    if not parsed.scheme or not parsed.netloc:
        raise HubRemoteError(f"invalid remote URL: {remote}")
    path = parsed.path.rstrip("/")
    if path in ("", "/"):
        path = "/mcp"
    elif path != "/mcp":
        path = f"{path}/mcp"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def call_hub_admin_tool(remote: str, admin_token: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    url = normalize_mcp_url(remote)
    headers = {"Authorization": f"Bearer {admin_token}", "Accept": "application/json, text/event-stream"}
    with httpx.Client(timeout=30.0) as client:
        session_id = _initialize(client, url, headers)
        headers["Mcp-Session-Id"] = session_id
        _notify_initialized(client, url, headers)
        envelope = _post_json_rpc(
            client,
            url,
            headers,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}},
        )
    return _extract_tool_payload(envelope)
```

Also add private helpers `_initialize`, `_notify_initialized`, `_post_json_rpc`, `_decode_sse_json`, and `_extract_tool_payload`. `_extract_tool_payload` must raise `HubRemoteError` when the MCP envelope has `error`, when tool text starts with `Error:`, or when content is not valid JSON.

- [ ] **Step 2: Wire remote options into hub commands**

Add `--remote` and `--admin-token` to the scoped commands only. Use:

```python
def _resolve_remote_admin_token(admin_token: str | None, remote: str | None) -> str | None:
    if not remote:
        return None
    token = admin_token or os.environ.get("NEXUS_HUB_ADMIN_TOKEN")
    if not token:
        raise click.ClickException("--admin-token or NEXUS_HUB_ADMIN_TOKEN is required with --remote")
    return token
```

In each command, branch before local DB work:

```python
if remote:
    token = _resolve_remote_admin_token(admin_token, remote)
    payload = call_hub_admin_tool(remote, token, "nexus_hub_token_list", {"show_revoked": show_revoked})
    _render_token_list(payload, as_json=as_json)
    return
```

Use equivalent tool names and argument dictionaries from the tests for create, revoke, and status.

- [ ] **Step 3: Verify remote CLI tests pass**

Run:

```bash
pytest tests/unit/cli/test_hub_remote.py -q
```

Expected: all tests pass.

## Task 5: Backend Hub Admin RPC Tests

**Files:**
- Create: `tests/unit/server/rpc/handlers/test_hub_admin.py`
- Later create: `src/nexus/server/rpc/handlers/hub_admin.py`
- Later modify: `src/nexus/server/_rpc_param_overrides.py`
- Later modify: `src/nexus/server/rpc/dispatch.py`

- [ ] **Step 1: Write failing backend RPC handler tests**

Add `tests/unit/server/rpc/handlers/test_hub_admin.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.contracts.exceptions import NexusPermissionError
from nexus.server.rpc.handlers import hub_admin


def test_hub_admin_list_requires_admin_before_operation(monkeypatch):
    called = False

    def fake_list(*args, **kwargs):
        nonlocal called
        called = True
        return {"tokens": []}

    monkeypatch.setattr(hub_admin.admin_ops, "list_hub_tokens", fake_list)

    with pytest.raises(NexusPermissionError):
        hub_admin.handle_hub_admin_token_list(
            SimpleNamespace(session_factory=lambda: None),
            SimpleNamespace(show_revoked=False),
            SimpleNamespace(is_admin=False, user_id="bob"),
        )

    assert called is False


def test_hub_admin_create_delegates_to_shared_ops(monkeypatch):
    calls = []

    def fake_create(session_factory, **kwargs):
        calls.append((session_factory, kwargs))
        return {"key_id": "nk_1", "token": "sk_1"}

    monkeypatch.setattr(hub_admin.admin_ops, "create_hub_token", fake_create)
    auth_provider = SimpleNamespace(session_factory="factory")

    result = hub_admin.handle_hub_admin_token_create(
        auth_provider,
        SimpleNamespace(name="ci", zones="eng:rw", zones_glob=None, admin=True, expires="7d", user_id="u1"),
        SimpleNamespace(is_admin=True, user_id="admin"),
    )

    assert result == {"key_id": "nk_1", "token": "sk_1"}
    assert calls == [
        (
            "factory",
            {"name": "ci", "zones_csv": "eng:rw", "zones_glob": None, "is_admin": True, "expires": "7d", "user_id": "u1"},
        )
    ]


def test_hub_admin_revoke_delegates_to_shared_ops(monkeypatch):
    calls = []

    def fake_revoke(session_factory, **kwargs):
        calls.append((session_factory, kwargs))
        return {"key_id": "nk_1", "name": "old", "message": "revoked old (nk_1). Effective within 60s (auth cache TTL)."}

    monkeypatch.setattr(hub_admin.admin_ops, "revoke_hub_token", fake_revoke)

    result = hub_admin.handle_hub_admin_token_revoke(
        SimpleNamespace(session_factory="factory"),
        SimpleNamespace(identifier="old"),
        SimpleNamespace(is_admin=True, user_id="admin"),
    )

    assert result["key_id"] == "nk_1"
    assert calls == [("factory", {"identifier": "old"})]
```

- [ ] **Step 2: Run the tests and verify they fail on missing handler module**

Run:

```bash
pytest tests/unit/server/rpc/handlers/test_hub_admin.py -q
```

Expected: import failure for `nexus.server.rpc.handlers.hub_admin`.

## Task 6: Backend Hub Admin RPC Implementation

**Files:**
- Create: `src/nexus/server/rpc/handlers/hub_admin.py`
- Modify: `src/nexus/server/_rpc_param_overrides.py`
- Modify: `src/nexus/server/rpc/dispatch.py`

- [ ] **Step 1: Implement handler module**

Create `src/nexus/server/rpc/handlers/hub_admin.py`:

```python
from __future__ import annotations

from typing import Any

from nexus.hub import admin_ops
from nexus.server.rpc.handlers.admin import require_admin, require_database_auth


def _session_factory(auth_provider: Any) -> Any:
    require_database_auth(auth_provider)
    return auth_provider.session_factory


def handle_hub_admin_token_create(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    require_admin(context)
    return admin_ops.create_hub_token(
        _session_factory(auth_provider),
        name=params.name,
        zones_csv=getattr(params, "zones", None),
        zones_glob=getattr(params, "zones_glob", None),
        is_admin=bool(getattr(params, "admin", False)),
        expires=getattr(params, "expires", None),
        user_id=getattr(params, "user_id", None),
    )


def handle_hub_admin_token_list(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    require_admin(context)
    return admin_ops.list_hub_tokens(_session_factory(auth_provider), show_revoked=bool(getattr(params, "show_revoked", False)))


def handle_hub_admin_token_revoke(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    require_admin(context)
    return admin_ops.revoke_hub_token(_session_factory(auth_provider), identifier=params.identifier)


def handle_hub_admin_status(auth_provider: Any, params: Any, context: Any) -> dict[str, Any]:
    require_admin(context)
    return admin_ops.get_hub_status(_session_factory(auth_provider))
```

- [ ] **Step 2: Add RPC params and dispatch entries**

In `src/nexus/server/_rpc_param_overrides.py`, add dataclasses and entries:

```python
@dataclass
class HubAdminTokenCreateParams:
    name: str
    zones: str | None = None
    zones_glob: str | None = None
    admin: bool = False
    expires: str | None = None
    user_id: str | None = None


@dataclass
class HubAdminTokenListParams:
    show_revoked: bool = False


@dataclass
class HubAdminTokenRevokeParams:
    identifier: str


@dataclass
class HubAdminStatusParams:
    pass
```

Add `OVERRIDE_METHOD_PARAMS` entries for `hub_admin_token_create`, `hub_admin_token_list`, `hub_admin_token_revoke`, and `hub_admin_status`.

In `src/nexus/server/rpc/dispatch.py`, import the four handlers inside `build_dispatch_table()` and add:

```python
"hub_admin_token_create": DispatchEntry(handle_hub_admin_token_create, pass_auth_provider=True),
"hub_admin_token_list": DispatchEntry(handle_hub_admin_token_list, pass_auth_provider=True),
"hub_admin_token_revoke": DispatchEntry(handle_hub_admin_token_revoke, pass_auth_provider=True),
"hub_admin_status": DispatchEntry(handle_hub_admin_status, pass_auth_provider=True),
```

- [ ] **Step 3: Verify backend RPC tests pass**

Run:

```bash
pytest tests/unit/server/rpc/handlers/test_hub_admin.py -q
```

Expected: all tests pass.

## Task 7: MCP Hub Admin Tool Tests

**Files:**
- Create: `tests/unit/bricks/mcp/test_hub_admin_tools.py`
- Later modify: `src/nexus/bricks/mcp/server.py`
- Later modify: `src/nexus/__init__.py`

- [ ] **Step 1: Write failing MCP tool tests**

Add `tests/unit/bricks/mcp/test_hub_admin_tools.py`:

```python
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nexus.bricks.mcp.server import create_mcp_server


async def _get_tool(server, name):
    tools = await server.get_tools()
    return tools[name].fn


@pytest.mark.asyncio
async def test_hub_admin_tool_forwards_to_remote_rpc():
    calls = []

    def fake_call_rpc(method, params):
        calls.append((method, params))
        return {"tokens": [{"key_id": "nk_1", "name": "admin"}]}

    nx = SimpleNamespace(is_admin=True, _nexus_remote_call_rpc=fake_call_rpc)
    server = await create_mcp_server(nx=nx)
    tool = await _get_tool(server, "nexus_hub_token_list")

    result = await tool(show_revoked=True)

    assert calls == [("hub_admin_token_list", {"show_revoked": True})]
    assert json.loads(result)["tokens"][0]["key_id"] == "nk_1"


@pytest.mark.asyncio
async def test_hub_admin_tool_rejects_non_admin_before_rpc():
    calls = []

    def fake_call_rpc(method, params):
        calls.append((method, params))
        return {"tokens": []}

    nx = SimpleNamespace(is_admin=False, _nexus_remote_call_rpc=fake_call_rpc)
    server = await create_mcp_server(nx=nx)
    tool = await _get_tool(server, "nexus_hub_token_list")

    result = await tool(show_revoked=False)

    assert calls == []
    assert result.startswith("Error:")
    assert "Admin privileges required" in result
```

- [ ] **Step 2: Run the tests and verify they fail because the MCP tools are not registered**

Run:

```bash
pytest tests/unit/bricks/mcp/test_hub_admin_tools.py -q
```

Expected: `KeyError: 'nexus_hub_token_list'`.

## Task 8: MCP Hub Admin Tool Implementation

**Files:**
- Modify: `src/nexus/bricks/mcp/server.py`
- Modify: `src/nexus/__init__.py`

- [ ] **Step 1: Expose remote RPC transport on remote NexusFS**

In the `cfg.profile == "remote"` branch of `src/nexus/__init__.py`, after `install_remote_kernel_rpc_overrides(nfs, transport)`, add:

```python
cast(Any, nfs)._nexus_remote_call_rpc = transport.call_rpc
```

This gives MCP tools a stable private hook to call backend hub-admin RPC methods through the same per-request remote connection they already use for filesystem operations.

- [ ] **Step 2: Register MCP admin tools**

In `src/nexus/bricks/mcp/server.py`, add a small helper near the tool definitions:

```python
def _require_hub_admin(nx_instance: Any) -> None:
    op_context = _resolve_mcp_operation_context(nx_instance, auth_provider=auth_provider)
    if not getattr(op_context, "is_admin", False):
        raise PermissionError("Admin privileges required for hub administration")
```

Add tools:

```python
@mcp.tool()
@handle_tool_errors
async def nexus_hub_token_list(show_revoked: bool = False) -> str:
    nx_instance = await _get_nexus_instance()
    _require_hub_admin(nx_instance)
    call_rpc = getattr(nx_instance, "_nexus_remote_call_rpc", None)
    if call_rpc is None:
        raise RuntimeError("hub admin tools require a remote hub backend")
    return json.dumps(call_rpc("hub_admin_token_list", {"show_revoked": show_revoked}))
```

Add equivalent tools for create, revoke, and status:

```python
call_rpc("hub_admin_token_create", {"name": name, "zones": zones, "zones_glob": zones_glob, "admin": admin, "expires": expires, "user_id": user_id})
call_rpc("hub_admin_token_revoke", {"identifier": identifier})
call_rpc("hub_admin_status", {})
```

- [ ] **Step 3: Verify MCP tool tests pass**

Run:

```bash
pytest tests/unit/bricks/mcp/test_hub_admin_tools.py -q
```

Expected: all tests pass.

## Task 9: Full Targeted Verification

**Files:**
- All files changed in Tasks 1-8.

- [ ] **Step 1: Run the targeted unit suite**

Run:

```bash
pytest \
  tests/unit/hub/test_admin_ops.py \
  tests/unit/cli/test_hub.py \
  tests/unit/cli/test_hub_remote.py \
  tests/unit/cli/test_hub_token_list_primary_alias.py \
  tests/unit/server/rpc/handlers/test_hub_admin.py \
  tests/unit/server/rpc/handlers/test_admin_primary_alias.py \
  tests/unit/server/rpc/handlers/test_admin_junction_filter.py \
  tests/unit/bricks/mcp/test_hub_admin_tools.py \
  tests/unit/bricks/mcp/test_mcp_server_tools.py \
  tests/unit/cli/test_mcp_embedded_hub_rejection.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run lint/type checks used by this repo if available**

Run:

```bash
python -m compileall src/nexus/hub src/nexus/cli/commands/_hub_remote.py src/nexus/server/rpc/handlers/hub_admin.py
```

Expected: command exits 0.

- [ ] **Step 3: Commit the tested implementation**

Run:

```bash
git status --short
git add src/nexus/hub src/nexus/cli/commands/_hub_remote.py src/nexus/cli/commands/hub.py src/nexus/bricks/mcp/server.py src/nexus/server/_rpc_param_overrides.py src/nexus/server/rpc/dispatch.py src/nexus/server/rpc/handlers/hub_admin.py src/nexus/__init__.py tests/unit/hub/test_admin_ops.py tests/unit/cli/test_hub_remote.py tests/unit/server/rpc/handlers/test_hub_admin.py tests/unit/bricks/mcp/test_hub_admin_tools.py
git commit -m "feat: add remote hub admin cli"
```

Expected: commit succeeds.

## Task 10: Stack Smoke Verification

**Files:**
- No source edits expected.

- [ ] **Step 1: Start the local hub stack from this branch**

Run:

```bash
nexus up --build
```

Expected: stack starts and prints MCP/RPC endpoint information. If port conflicts occur, stop the conflicting prior stack with the repo's documented stop command and rerun `nexus up --build`.

- [ ] **Step 2: Bootstrap an admin token locally**

Run with the stack's database URL from the generated environment:

```bash
nexus hub token create --name workstation-admin --admin
```

Expected: output contains `key_id:` and one-time `token:`.

- [ ] **Step 3: Verify remote list, create, revoke, and status**

Run with the token from Step 2:

```bash
nexus hub token list --remote http://localhost:8081 --admin-token <admin-token>
nexus hub token create --remote http://localhost:8081 --admin-token <admin-token> --name remote-ci --zones '*'
nexus hub token revoke remote-ci --remote http://localhost:8081 --admin-token <admin-token>
nexus hub status --remote http://localhost:8081 --admin-token <admin-token>
```

Expected: list and status exit 0, create prints a one-time token, revoke prints the standard revoke message.

- [ ] **Step 4: Verify non-admin denial**

Use the non-admin token created in Step 3:

```bash
nexus hub token list --remote http://localhost:8081 --admin-token <non-admin-token>
```

Expected: command exits non-zero and includes `Admin privileges required`.
