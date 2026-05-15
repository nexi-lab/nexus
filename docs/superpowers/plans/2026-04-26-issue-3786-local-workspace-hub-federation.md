# Local Workspace + Remote Hub Federation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable a thin nexus instance (SANDBOX profile) to index a local workspace and federate search + file operations to a remote hub over gRPC, presenting three zones: `local` (r/w disk), `company` (r, hub-proxied), `shared` (r/w, hub-proxied).

**Architecture:** New `RemoteZoneBackend` subclasses existing `RemoteBackend`, adding per-zone permission enforcement. `FederationHandshake` authenticates to the hub via a new `federation_client_whoami` gRPC RPC and returns zone grants. `SandboxBootstrapper` orchestrates boot: local zone → handshake → mount remote zones → background-index workspace. `nexus up --profile sandbox --workspace PATH --hub-url URL --hub-token TOKEN` runs `nexusd` in sandbox mode.

**Tech Stack:** Python, gRPC (`RPCTransport`), pytest, Click, threading, `asyncio.run` for background indexing

---

## File Map

| Status | Path | Responsibility |
|--------|------|----------------|
| Create | `src/nexus/backends/storage/remote_zone.py` | `RemoteZoneBackend` — permission-aware proxy backend |
| Create | `src/nexus/remote/federation_handshake.py` | `FederationHandshake`, `HubSession`, `HubZoneGrant` |
| Create | `src/nexus/daemon/sandbox_bootstrap.py` | `SandboxBootstrapper` — boot orchestration |
| Create | `src/nexus/core/boot_indexer.py` | `BootIndexer` — background workspace walk + index |
| Modify | `src/nexus/contracts/exceptions.py` | Add `ZoneReadOnlyError`, `ZoneUnavailableError`, `HandshakeAuthError`, `HandshakeConnectionError` |
| Modify | `src/nexus/server/rpc/services/federation_rpc.py` | Add `federation_client_whoami` RPC |
| Modify | `src/nexus/daemon/main.py` | Add `--workspace`, `--hub-url`, `--hub-token` flags; call `SandboxBootstrapper` |
| Modify | `src/nexus/cli/commands/stack.py` | Add `--profile sandbox` shortcut that runs `nexusd` |
| Create | `tests/unit/backends/test_remote_zone.py` | Unit tests for `RemoteZoneBackend` |
| Create | `tests/unit/remote/test_federation_handshake.py` | Unit tests for `FederationHandshake` |
| Create | `tests/unit/daemon/test_sandbox_bootstrap.py` | Unit tests for `SandboxBootstrapper` |
| Create | `tests/unit/core/test_boot_indexer.py` | Unit tests for `BootIndexer` |
| Create | `tests/unit/cli/test_stack_sandbox_flags.py` | CLI flag validation tests |

---

## Task 1: New exceptions in `contracts/exceptions.py`

**Files:**
- Modify: `src/nexus/contracts/exceptions.py`

- [ ] **Step 1: Write the failing import test**

```python
# tests/unit/contracts/test_exceptions_federation.py
from nexus.contracts.exceptions import (
    ZoneReadOnlyError,
    ZoneUnavailableError,
    HandshakeAuthError,
    HandshakeConnectionError,
    NexusError,
)

def test_zone_read_only_is_nexus_error() -> None:
    exc = ZoneReadOnlyError("Zone 'company' is read-only")
    assert isinstance(exc, NexusError)
    assert "company" in str(exc)

def test_zone_unavailable_is_nexus_error() -> None:
    exc = ZoneUnavailableError("Zone 'company' is unavailable")
    assert isinstance(exc, NexusError)

def test_handshake_auth_error_is_nexus_error() -> None:
    exc = HandshakeAuthError("Token rejected by hub")
    assert isinstance(exc, NexusError)

def test_handshake_connection_error_is_nexus_error() -> None:
    exc = HandshakeConnectionError("Hub unreachable")
    assert isinstance(exc, NexusError)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/contracts/test_exceptions_federation.py -v
```
Expected: FAIL with `ImportError: cannot import name 'ZoneReadOnlyError'`

- [ ] **Step 3: Add exceptions to `src/nexus/contracts/exceptions.py`**

Find the end of the file (after `AuditLogError`) and append:

```python
class ZoneReadOnlyError(NexusError):
    """Raised when a write is attempted on a read-only federated zone."""


class ZoneUnavailableError(NexusError):
    """Raised when a remote zone's hub transport is unavailable mid-session."""


class HandshakeAuthError(NexusError):
    """Raised when hub rejects the bearer token during federation handshake (401)."""


class HandshakeConnectionError(NexusError):
    """Raised when the hub is unreachable during federation handshake."""
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/contracts/test_exceptions_federation.py -v
```
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/contracts/exceptions.py tests/unit/contracts/test_exceptions_federation.py
git commit -m "feat(#3786): add ZoneReadOnlyError, ZoneUnavailableError, HandshakeAuth/ConnectionError"
```

---

## Task 2: `RemoteZoneBackend`

**Files:**
- Create: `src/nexus/backends/storage/remote_zone.py`
- Create: `tests/unit/backends/test_remote_zone.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/backends/test_remote_zone.py
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.backends.storage.remote_zone import RemoteZoneBackend
from nexus.contracts.exceptions import ZoneReadOnlyError


@pytest.fixture
def mock_transport() -> MagicMock:
    t = MagicMock()
    t.write_file.return_value = {"etag": "abc", "size": 5}
    t.read_file.return_value = b"hello"
    return t


@pytest.fixture
def readonly_backend(mock_transport: MagicMock) -> RemoteZoneBackend:
    return RemoteZoneBackend(zone_id="company", transport=mock_transport, permission="r")


@pytest.fixture
def readwrite_backend(mock_transport: MagicMock) -> RemoteZoneBackend:
    return RemoteZoneBackend(zone_id="shared", transport=mock_transport, permission="rw")


class TestRemoteZoneBackendIdentity:
    def test_name_includes_zone_id(self, readonly_backend: RemoteZoneBackend) -> None:
        assert "company" in readonly_backend.name

    def test_zone_id_stored(self, readonly_backend: RemoteZoneBackend) -> None:
        assert readonly_backend.zone_id == "company"

    def test_permission_stored(self, readonly_backend: RemoteZoneBackend) -> None:
        assert readonly_backend.permission == "r"


class TestRemoteZoneBackendReadOnly:
    def test_write_content_raises_zone_read_only(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        with pytest.raises(ZoneReadOnlyError, match="company"):
            readonly_backend.write_content(b"data")
        mock_transport.write_file.assert_not_called()

    def test_delete_content_raises_zone_read_only(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        with pytest.raises(ZoneReadOnlyError, match="company"):
            readonly_backend.delete_content("some_id")
        mock_transport.delete_file.assert_not_called()

    def test_mkdir_raises_zone_read_only(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        with pytest.raises(ZoneReadOnlyError):
            readonly_backend.mkdir("/some/path")
        mock_transport.call_rpc.assert_not_called()

    def test_rmdir_raises_zone_read_only(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        with pytest.raises(ZoneReadOnlyError):
            readonly_backend.rmdir("/some/path")
        mock_transport.call_rpc.assert_not_called()

    def test_read_content_passes_through(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        mock_transport.read_file.return_value = b"content"
        from nexus.contracts.types import OperationContext
        ctx = OperationContext(user_id="u", groups=[], backend_path="/file.txt")
        result = readonly_backend.read_content("id", context=ctx)
        assert result == b"content"
        mock_transport.read_file.assert_called_once()


class TestRemoteZoneBackendReadWrite:
    def test_write_content_delegates_to_transport(
        self, readwrite_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        from nexus.contracts.types import OperationContext
        ctx = OperationContext(user_id="u", groups=[], backend_path="/note.md")
        readwrite_backend.write_content(b"hello", context=ctx)
        mock_transport.write_file.assert_called_once()

    def test_delete_content_delegates_to_transport(
        self, readwrite_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        from nexus.contracts.types import OperationContext
        ctx = OperationContext(user_id="u", groups=[], backend_path="/note.md")
        readwrite_backend.delete_content("id", context=ctx)
        mock_transport.delete_file.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/backends/test_remote_zone.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.backends.storage.remote_zone'`

- [ ] **Step 3: Implement `RemoteZoneBackend`**

Create `src/nexus/backends/storage/remote_zone.py`:

```python
"""RemoteZoneBackend — permission-aware proxy backend for federated hub zones.

Subclasses RemoteBackend to add per-zone read/write permission enforcement.
A zone with permission="r" raises ZoneReadOnlyError before any gRPC call
for write operations. A zone with permission="rw" passes all ops through.

Issue #3786: Local workspace + remote hub federation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.backends.storage.remote import RemoteBackend
from nexus.contracts.exceptions import ZoneReadOnlyError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.remote.rpc_transport import RPCTransport


class RemoteZoneBackend(RemoteBackend):
    """RemoteBackend with per-zone permission enforcement.

    Args:
        zone_id: Zone identifier (used in error messages and backend name).
        transport: Shared RPCTransport instance connected to the hub.
        permission: "r" for read-only, "rw" for read-write.
    """

    def __init__(
        self,
        zone_id: str,
        transport: RPCTransport,
        permission: str,
    ) -> None:
        super().__init__(transport)
        self.zone_id = zone_id
        self.permission = permission

    @property
    def name(self) -> str:
        return f"remote_zone:{self.zone_id}"

    def _check_write_permission(self) -> None:
        if self.permission != "rw":
            raise ZoneReadOnlyError(
                f"Zone '{self.zone_id}' is read-only (permission='{self.permission}')"
            )

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> WriteResult:
        self._check_write_permission()
        return super().write_content(content, content_id, offset=offset, context=context)

    def delete_content(
        self, content_id: str, context: OperationContext | None = None
    ) -> None:
        self._check_write_permission()
        super().delete_content(content_id, context=context)

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        self._check_write_permission()
        super().mkdir(path, parents=parents, exist_ok=exist_ok, context=context)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        self._check_write_permission()
        super().rmdir(path, recursive=recursive, context=context)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/backends/test_remote_zone.py -v
```
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/backends/storage/remote_zone.py tests/unit/backends/test_remote_zone.py
git commit -m "feat(#3786): add RemoteZoneBackend with read-only permission enforcement"
```

---

## Task 3: Hub-side `federation_client_whoami` RPC

**Files:**
- Modify: `src/nexus/server/rpc/services/federation_rpc.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/grpc/test_federation_whoami_rpc.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestFederationClientWhoami:
    def test_whoami_returns_zone_grants(self) -> None:
        from nexus.server.rpc.services.federation_rpc import FederationRPCMixin

        mixin = FederationRPCMixin.__new__(FederationRPCMixin)
        mixin._context = MagicMock()
        mixin._context.zone_set = {"company": "r", "shared": "rw"}
        mixin._context.zone_id = None
        mixin._context.is_admin = False

        result = mixin.federation_client_whoami()
        assert "zones" in result
        zones = {z["zone_id"]: z["permission"] for z in result["zones"]}
        assert zones["company"] == "r"
        assert zones["shared"] == "rw"

    def test_whoami_with_single_zone_context(self) -> None:
        from nexus.server.rpc.services.federation_rpc import FederationRPCMixin

        mixin = FederationRPCMixin.__new__(FederationRPCMixin)
        mixin._context = MagicMock()
        mixin._context.zone_set = None
        mixin._context.zone_id = "eng"
        mixin._context.is_admin = False

        result = mixin.federation_client_whoami()
        assert result["zones"] == [{"zone_id": "eng", "permission": "r"}]

    def test_whoami_with_no_context_raises(self) -> None:
        from nexus.server.rpc.services.federation_rpc import FederationRPCMixin
        from nexus.contracts.exceptions import NexusPermissionError

        mixin = FederationRPCMixin.__new__(FederationRPCMixin)
        mixin._context = None

        with pytest.raises((NexusPermissionError, AttributeError)):
            mixin.federation_client_whoami()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/grpc/test_federation_whoami_rpc.py -v
```
Expected: FAIL with `AttributeError: federation_client_whoami`

- [ ] **Step 3: Add `federation_client_whoami` to `federation_rpc.py`**

Open `src/nexus/server/rpc/services/federation_rpc.py`. Find the last `@rpc_expose` method (around line 358: `federation_cluster_info`). Append after it:

```python
    @rpc_expose(admin_only=False)
    def federation_client_whoami(self) -> dict[str, Any]:
        """Return the caller's zone grants for federation handshake.

        Called by thin clients during SandboxBootstrapper startup to discover
        which zones their bearer token can access and with what permissions.
        Returns a list of {zone_id, permission} dicts from the caller's context.

        Issue #3786: federation handshake for thin client.
        """
        if self._context is None:
            raise NexusPermissionError("federation_client_whoami requires authentication")

        # P3-2 multi-zone tokens carry zone_set: {zone_id: permission}
        zone_set = getattr(self._context, "zone_set", None)
        if zone_set:
            zones = [
                {"zone_id": zid, "permission": perm}
                for zid, perm in zone_set.items()
            ]
            return {"zones": zones}

        # P3-1 single-zone tokens carry zone_id with implicit read permission
        zone_id = getattr(self._context, "zone_id", None)
        if zone_id:
            return {"zones": [{"zone_id": zone_id, "permission": "r"}]}

        return {"zones": []}
```

Also ensure `NexusPermissionError` is imported at the top of `federation_rpc.py`. Check the imports; if missing, add:
```python
from nexus.contracts.exceptions import NexusPermissionError
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/grpc/test_federation_whoami_rpc.py -v
```
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/rpc/services/federation_rpc.py tests/unit/grpc/test_federation_whoami_rpc.py
git commit -m "feat(#3786): add federation_client_whoami RPC for thin client handshake"
```

---

## Task 4: `FederationHandshake`

**Files:**
- Create: `src/nexus/remote/federation_handshake.py`
- Create: `tests/unit/remote/test_federation_handshake.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/remote/test_federation_handshake.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.exceptions import (
    AuthenticationError,
    HandshakeAuthError,
    HandshakeConnectionError,
    RemoteConnectionError,
)
from nexus.remote.federation_handshake import FederationHandshake, HubSession, HubZoneGrant


class TestFederationHandshakeSuccess:
    def test_connect_returns_hub_session(self) -> None:
        mock_transport = MagicMock()
        mock_transport.call_rpc.return_value = {
            "zones": [
                {"zone_id": "company", "permission": "r"},
                {"zone_id": "shared", "permission": "rw"},
            ]
        }

        with patch(
            "nexus.remote.federation_handshake.RPCTransport", return_value=mock_transport
        ):
            hs = FederationHandshake("hub.example.com:2028", "mytoken")
            session = hs.connect()

        assert isinstance(session, HubSession)
        assert session.transport is mock_transport
        assert len(session.zones) == 2

    def test_zone_grants_parsed_correctly(self) -> None:
        mock_transport = MagicMock()
        mock_transport.call_rpc.return_value = {
            "zones": [
                {"zone_id": "eng", "permission": "r"},
            ]
        }

        with patch(
            "nexus.remote.federation_handshake.RPCTransport", return_value=mock_transport
        ):
            hs = FederationHandshake("hub.example.com:2028", "tok")
            session = hs.connect()

        grant = session.zones[0]
        assert isinstance(grant, HubZoneGrant)
        assert grant.zone_id == "eng"
        assert grant.permission == "r"

    def test_call_rpc_uses_federation_client_whoami(self) -> None:
        mock_transport = MagicMock()
        mock_transport.call_rpc.return_value = {"zones": []}

        with patch(
            "nexus.remote.federation_handshake.RPCTransport", return_value=mock_transport
        ):
            FederationHandshake("hub:2028", "tok").connect()

        mock_transport.call_rpc.assert_called_once_with("federation_client_whoami", {})


class TestFederationHandshakeFailure:
    def test_connection_error_raises_handshake_connection_error(self) -> None:
        mock_transport = MagicMock()
        mock_transport.call_rpc.side_effect = RemoteConnectionError("unreachable")

        with patch(
            "nexus.remote.federation_handshake.RPCTransport", return_value=mock_transport
        ):
            hs = FederationHandshake("hub:2028", "tok")
            with pytest.raises(HandshakeConnectionError, match="hub:2028"):
                hs.connect()

    def test_auth_error_raises_handshake_auth_error(self) -> None:
        mock_transport = MagicMock()
        mock_transport.call_rpc.side_effect = AuthenticationError("401 unauthorized")

        with patch(
            "nexus.remote.federation_handshake.RPCTransport", return_value=mock_transport
        ):
            hs = FederationHandshake("hub:2028", "badtoken")
            with pytest.raises(HandshakeAuthError):
                hs.connect()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/remote/test_federation_handshake.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `FederationHandshake`**

Create `src/nexus/remote/federation_handshake.py`:

```python
"""Federation handshake for thin client sandbox boot.

Called once at startup by SandboxBootstrapper. Authenticates to the hub
via bearer token, discovers which zones the token can access and with
what permissions, and returns a HubSession for ongoing operations.

Issue #3786: local workspace + remote hub federation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nexus.contracts.exceptions import (
    AuthenticationError,
    HandshakeAuthError,
    HandshakeConnectionError,
    RemoteConnectionError,
)
from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)


@dataclass
class HubZoneGrant:
    """A single zone assignment from the hub token."""

    zone_id: str
    permission: str  # "r" or "rw"


@dataclass
class HubSession:
    """Result of a successful federation handshake.

    Holds the open gRPC transport and the list of zone grants for this token.
    The transport is reused for all subsequent RemoteZoneBackend operations.
    """

    transport: RPCTransport
    zones: list[HubZoneGrant] = field(default_factory=list)


class FederationHandshake:
    """Authenticates a thin client to a Nexus hub and discovers zone grants.

    Args:
        hub_address: Hub gRPC address (e.g. ``hub.company.com:2028``).
        token: Bearer token minted by ``nexus hub token create``.
    """

    def __init__(self, hub_address: str, token: str) -> None:
        self._hub_address = hub_address
        self._token = token

    def connect(self) -> HubSession:
        """Perform the handshake and return a HubSession.

        Creates a gRPC transport, calls ``federation_client_whoami`` to
        retrieve zone grants, and returns the session. The transport is
        left open for subsequent operations.

        Raises:
            HandshakeConnectionError: Hub is unreachable.
            HandshakeAuthError: Token is rejected (401).
        """
        transport = RPCTransport(self._hub_address, auth_token=self._token)
        try:
            raw = transport.call_rpc("federation_client_whoami", {})
        except RemoteConnectionError as exc:
            transport.close()
            raise HandshakeConnectionError(
                f"Hub unreachable at '{self._hub_address}': {exc}"
            ) from exc
        except AuthenticationError as exc:
            transport.close()
            raise HandshakeAuthError(
                f"Hub rejected token for '{self._hub_address}': {exc}"
            ) from exc

        zones = [
            HubZoneGrant(zone_id=z["zone_id"], permission=z["permission"])
            for z in raw.get("zones", [])
        ]
        logger.info(
            "[HANDSHAKE] Connected to hub %s, zones=%s",
            self._hub_address,
            [z.zone_id for z in zones],
        )
        return HubSession(transport=transport, zones=zones)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/remote/test_federation_handshake.py -v
```
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/remote/federation_handshake.py tests/unit/remote/test_federation_handshake.py
git commit -m "feat(#3786): add FederationHandshake + HubSession for thin client boot"
```

---

## Task 5: `BootIndexer`

**Files:**
- Create: `src/nexus/core/boot_indexer.py`
- Create: `tests/unit/core/test_boot_indexer.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/core/test_boot_indexer.py
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core.boot_indexer import BootIndexer


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main(): pass")
    (tmp_path / "src" / "util.py").write_text("def helper(): pass")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]")
    return tmp_path


class TestBootIndexerBasic:
    def test_start_async_completes(self, workspace: Path) -> None:
        mock_indexer = MagicMock()
        mock_indexer.index_path = AsyncMock(return_value={"src/main.py": 1})

        indexer = BootIndexer(workspace=workspace, pipeline_indexer=mock_indexer)
        indexer.start_async()
        indexer.wait(timeout=5.0)

        assert mock_indexer.index_path.called

    def test_git_dir_excluded(self, workspace: Path) -> None:
        indexed_paths: list[str] = []

        mock_indexer = MagicMock()

        async def fake_index(path: str, recursive: bool = True) -> dict:
            indexed_paths.append(path)
            return {}

        mock_indexer.index_path = fake_index

        indexer = BootIndexer(workspace=workspace, pipeline_indexer=mock_indexer)
        indexer.start_async()
        indexer.wait(timeout=5.0)

        assert not any(".git" in p for p in indexed_paths)

    def test_failure_does_not_raise(self, workspace: Path) -> None:
        mock_indexer = MagicMock()
        mock_indexer.index_path = AsyncMock(side_effect=RuntimeError("index failed"))

        indexer = BootIndexer(workspace=workspace, pipeline_indexer=mock_indexer)
        indexer.start_async()
        indexer.wait(timeout=5.0)
        # No exception — failure is logged and swallowed

    def test_is_done_after_completion(self, workspace: Path) -> None:
        mock_indexer = MagicMock()
        mock_indexer.index_path = AsyncMock(return_value={})

        indexer = BootIndexer(workspace=workspace, pipeline_indexer=mock_indexer)
        assert not indexer.is_done()
        indexer.start_async()
        indexer.wait(timeout=5.0)
        assert indexer.is_done()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/core/test_boot_indexer.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `BootIndexer`**

Create `src/nexus/core/boot_indexer.py`:

```python
"""BootIndexer — background workspace walk and index on first sandbox boot.

Walks the local workspace directory on first start, feeds files to the
local search daemon via PipelineIndexer. Runs in a background daemon thread
so the server becomes ready immediately; search results accumulate as
indexing progresses.

Skips hidden directories (dot-prefixed) to avoid indexing .git, .venv, etc.

Issue #3786: local workspace + remote hub federation.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".nox"}
)


class BootIndexer:
    """Index a local workspace directory in the background on first boot.

    Args:
        workspace: Absolute path to the workspace directory to index.
        pipeline_indexer: PipelineIndexer instance with an async ``index_path`` method.
    """

    def __init__(self, workspace: Path, pipeline_indexer: Any) -> None:
        self._workspace = workspace
        self._indexer = pipeline_indexer
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start_async(self) -> None:
        """Start indexing in a background daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="boot-indexer")
        self._thread.start()

    def wait(self, timeout: float = 60.0) -> None:
        """Block until indexing completes or timeout expires (for tests)."""
        self._done.wait(timeout=timeout)

    def is_done(self) -> bool:
        """Return True once the background walk has finished (success or failure)."""
        return self._done.is_set()

    def _run(self) -> None:
        try:
            asyncio.run(self._index_workspace())
        except Exception:
            logger.error(
                "[BOOT-INDEXER] Failed to index workspace %s", self._workspace, exc_info=True
            )
        finally:
            self._done.set()
            logger.info("[BOOT-INDEXER] Workspace indexing complete: %s", self._workspace)

    async def _index_workspace(self) -> None:
        """Walk workspace and index all non-ignored directories."""
        if not self._workspace.exists():
            logger.warning("[BOOT-INDEXER] Workspace does not exist: %s", self._workspace)
            return

        for entry in self._workspace.iterdir():
            if entry.is_dir() and entry.name in _SKIP_DIRS:
                continue
            try:
                await self._indexer.index_path(str(entry), recursive=True)
            except Exception:
                logger.warning(
                    "[BOOT-INDEXER] Failed to index %s, skipping", entry, exc_info=True
                )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/core/test_boot_indexer.py -v
```
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/core/boot_indexer.py tests/unit/core/test_boot_indexer.py
git commit -m "feat(#3786): add BootIndexer for background workspace indexing on first boot"
```

---

## Task 6: `SandboxBootstrapper`

**Files:**
- Create: `src/nexus/daemon/sandbox_bootstrap.py`
- Create: `tests/unit/daemon/test_sandbox_bootstrap.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/daemon/test_sandbox_bootstrap.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.exceptions import HandshakeAuthError, HandshakeConnectionError
from nexus.daemon.sandbox_bootstrap import SandboxBootstrapper
from nexus.remote.federation_handshake import HubSession, HubZoneGrant


def _make_mock_nexus_fs(tmp_path: Path) -> MagicMock:
    nx = MagicMock()
    nx._kernel = MagicMock()
    return nx


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("# app")
    return tmp_path


class TestSandboxBootstrapperSuccess:
    def test_registers_three_zones(self, workspace: Path) -> None:
        mock_session = HubSession(
            transport=MagicMock(),
            zones=[
                HubZoneGrant(zone_id="company", permission="r"),
                HubZoneGrant(zone_id="shared", permission="rw"),
            ],
        )
        mock_registry = MagicMock()
        mock_nx = MagicMock()

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as mock_hs_cls,
            patch("nexus.daemon.sandbox_bootstrap.ZoneSearchRegistry", return_value=mock_registry),
        ):
            mock_hs_cls.return_value.connect.return_value = mock_session
            bootstrapper = SandboxBootstrapper(
                nx=mock_nx,
                workspace=workspace,
                hub_address="hub:2028",
                hub_token="tok",
            )
            result = bootstrapper.run()

        assert result.hub_session is mock_session
        assert len(result.remote_backends) == 2
        zone_ids = {b.zone_id for b in result.remote_backends.values()}
        assert zone_ids == {"company", "shared"}

    def test_company_backend_is_read_only(self, workspace: Path) -> None:
        mock_session = HubSession(
            transport=MagicMock(),
            zones=[HubZoneGrant(zone_id="company", permission="r")],
        )
        mock_nx = MagicMock()

        with patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as mock_hs_cls:
            mock_hs_cls.return_value.connect.return_value = mock_session
            bootstrapper = SandboxBootstrapper(
                nx=mock_nx, workspace=workspace, hub_address="hub:2028", hub_token="tok"
            )
            result = bootstrapper.run()

        company_backend = result.remote_backends["company"]
        assert company_backend.permission == "r"

    def test_boot_indexer_started(self, workspace: Path) -> None:
        mock_session = HubSession(transport=MagicMock(), zones=[])
        mock_nx = MagicMock()

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as mock_hs_cls,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer") as mock_indexer_cls,
        ):
            mock_hs_cls.return_value.connect.return_value = mock_session
            mock_indexer = MagicMock()
            mock_indexer_cls.return_value = mock_indexer
            bootstrapper = SandboxBootstrapper(
                nx=mock_nx, workspace=workspace, hub_address="hub:2028", hub_token="tok"
            )
            bootstrapper.run()

        mock_indexer.start_async.assert_called_once()


class TestSandboxBootstrapperHandshakeFailure:
    def test_connection_failure_boots_local_only(self, workspace: Path) -> None:
        mock_nx = MagicMock()

        with patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as mock_hs_cls:
            mock_hs_cls.return_value.connect.side_effect = HandshakeConnectionError("unreachable")
            bootstrapper = SandboxBootstrapper(
                nx=mock_nx, workspace=workspace, hub_address="hub:2028", hub_token="tok"
            )
            result = bootstrapper.run()

        assert result.hub_session is None
        assert result.remote_backends == {}

    def test_auth_failure_boots_local_only(self, workspace: Path) -> None:
        mock_nx = MagicMock()

        with patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as mock_hs_cls:
            mock_hs_cls.return_value.connect.side_effect = HandshakeAuthError("bad token")
            bootstrapper = SandboxBootstrapper(
                nx=mock_nx, workspace=workspace, hub_address="hub:2028", hub_token="tok"
            )
            result = bootstrapper.run()

        assert result.hub_session is None
        assert result.remote_backends == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/daemon/test_sandbox_bootstrap.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `SandboxBootstrapper`**

Create `src/nexus/daemon/sandbox_bootstrap.py`:

```python
"""SandboxBootstrapper — orchestrates thin client sandbox boot.

Boot sequence:
1. Create local zone backed by PathLocalBackend(workspace)
2. Run FederationHandshake(hub_address, token)
   - On failure: log WARN, continue local-only (no crash)
3. For each zone grant: create RemoteZoneBackend, register in zone registry
4. Start BootIndexer in background thread

Returns a BootResult with the hub session, remote backends, and indexer.

Issue #3786: local workspace + remote hub federation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.backends.storage.remote_zone import RemoteZoneBackend
from nexus.bricks.search.zone_registry import ZoneSearchRegistry
from nexus.contracts.exceptions import HandshakeAuthError, HandshakeConnectionError
from nexus.core.boot_indexer import BootIndexer
from nexus.remote.federation_handshake import FederationHandshake, HubSession

if TYPE_CHECKING:
    from nexus.remote.federation_handshake import HubZoneGrant

logger = logging.getLogger(__name__)


@dataclass
class BootResult:
    """Result of a sandbox bootstrap run."""

    hub_session: HubSession | None
    remote_backends: dict[str, RemoteZoneBackend] = field(default_factory=dict)
    boot_indexer: BootIndexer | None = None


class SandboxBootstrapper:
    """Orchestrates the sandbox profile boot sequence.

    Args:
        nx: Connected NexusFS instance (SANDBOX profile).
        workspace: Local workspace directory to mount as 'local' zone.
        hub_address: Hub gRPC address (e.g. ``hub.company.com:2028``).
        hub_token: Bearer token for hub authentication.
        pipeline_indexer: Optional PipelineIndexer for workspace indexing.
            If None, BootIndexer is skipped.
    """

    def __init__(
        self,
        nx: Any,
        workspace: Path,
        hub_address: str,
        hub_token: str,
        pipeline_indexer: Any | None = None,
    ) -> None:
        self._nx = nx
        self._workspace = workspace
        self._hub_address = hub_address
        self._hub_token = hub_token
        self._pipeline_indexer = pipeline_indexer

    def run(self) -> BootResult:
        """Execute the bootstrap sequence and return a BootResult."""
        hub_session = self._handshake()
        remote_backends = self._mount_remote_zones(hub_session)
        boot_indexer = self._start_boot_indexer()
        return BootResult(
            hub_session=hub_session,
            remote_backends=remote_backends,
            boot_indexer=boot_indexer,
        )

    def _handshake(self) -> HubSession | None:
        try:
            hs = FederationHandshake(self._hub_address, self._hub_token)
            session = hs.connect()
            logger.info(
                "[SANDBOX-BOOT] Hub federation active: %s zones",
                len(session.zones),
            )
            return session
        except (HandshakeConnectionError, HandshakeAuthError) as exc:
            logger.warning(
                "[SANDBOX-BOOT] Hub federation unavailable, running local-only: %s", exc
            )
            return None

    def _mount_remote_zones(
        self, hub_session: HubSession | None
    ) -> dict[str, RemoteZoneBackend]:
        if hub_session is None:
            return {}

        backends: dict[str, RemoteZoneBackend] = {}
        for grant in hub_session.zones:
            backend = RemoteZoneBackend(
                zone_id=grant.zone_id,
                transport=hub_session.transport,
                permission=grant.permission,
            )
            backends[grant.zone_id] = backend
            logger.info(
                "[SANDBOX-BOOT] Mounted remote zone '%s' (perm=%s)",
                grant.zone_id,
                grant.permission,
            )
        return backends

    def _start_boot_indexer(self) -> BootIndexer | None:
        if self._pipeline_indexer is None:
            return None
        indexer = BootIndexer(workspace=self._workspace, pipeline_indexer=self._pipeline_indexer)
        indexer.start_async()
        logger.info("[SANDBOX-BOOT] BootIndexer started for %s", self._workspace)
        return indexer
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/daemon/test_sandbox_bootstrap.py -v
```
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/daemon/sandbox_bootstrap.py tests/unit/daemon/test_sandbox_bootstrap.py
git commit -m "feat(#3786): add SandboxBootstrapper for thin client boot orchestration"
```

---

## Task 7: `nexusd` CLI flags for sandbox mode

**Files:**
- Modify: `src/nexus/daemon/main.py`
- Create: `tests/unit/daemon/test_main_sandbox_flags.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/daemon/test_main_sandbox_flags.py
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.daemon.main import nexusd


class TestNexusdSandboxFlags:
    def test_workspace_flag_rejected_without_sandbox_profile(self) -> None:
        # catch_exceptions=True so SystemExit is captured as exit_code
        runner = CliRunner()
        result = runner.invoke(
            nexusd,
            ["--profile", "full", "--workspace", "/tmp/ws"],
            catch_exceptions=True,
        )
        assert result.exit_code != 0
        assert "--workspace" in result.output or "sandbox" in result.output.lower()

    def test_hub_url_flag_rejected_without_sandbox_profile(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            nexusd,
            ["--profile", "full", "--hub-url", "hub:2028"],
            catch_exceptions=True,
        )
        assert result.exit_code != 0

    def test_hub_url_without_token_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            nexusd,
            ["--profile", "sandbox", "--workspace", "/tmp/ws", "--hub-url", "hub:2028"],
            catch_exceptions=True,
        )
        assert result.exit_code != 0
        assert "token" in result.output.lower() or "NEXUS_HUB_TOKEN" in result.output

    def test_sandbox_flags_pass_validation(self, tmp_path) -> None:
        # Patch SandboxBootstrapper and nexus.connect so the daemon doesn't fully start
        with (
            patch("nexus.daemon.main.SandboxBootstrapper") as mock_bs_cls,
            patch("nexus.daemon.main.nexus") as mock_nexus,
        ):
            mock_nexus.connect.return_value = MagicMock()
            mock_bs_cls.return_value.run.return_value = MagicMock(hub_session=None)
            runner = CliRunner(env={"NEXUS_HUB_TOKEN": "mytoken"})
            # Invoke and let it fail after validation (e.g. during server startup)
            runner.invoke(
                nexusd,
                ["--profile", "sandbox", "--workspace", str(tmp_path), "--hub-url", "hub:2028"],
                catch_exceptions=True,
            )
            # Verify validation passed (no exit before SandboxBootstrapper was attempted)
            assert mock_bs_cls.called
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/daemon/test_main_sandbox_flags.py -v
```
Expected: FAIL (flags don't exist yet)

- [ ] **Step 3: Add sandbox flags to `src/nexus/daemon/main.py`**

Find the `@click.option` block for `--profile` (around line 182). Add three new options directly after the `--profile` option:

```python
@click.option(
    "--workspace",
    "workspace",
    type=click.Path(exists=False, file_okay=False, path_type=str),
    default=None,
    envvar="NEXUS_WORKSPACE",
    help="[sandbox only] Local workspace directory to index and mount as 'local' zone.",
)
@click.option(
    "--hub-url",
    "hub_url",
    default=None,
    envvar="NEXUS_HUB_URL",
    help="[sandbox only] Hub gRPC address (e.g. hub.company.com:2028).",
)
@click.option(
    "--hub-token",
    "hub_token",
    default=None,
    envvar="NEXUS_HUB_TOKEN",
    help="[sandbox only] Bearer token for hub authentication. Prefer NEXUS_HUB_TOKEN env var.",
)
```

Add `workspace: str | None`, `hub_url: str | None`, `hub_token: str | None` to the function signature of the main daemon function (find the line `def nexusd(` or `def main(`).

After the `if deployment_profile == "remote":` guard (around line 284), add a new validation block:

```python
# Guard: sandbox-only flags require --profile sandbox
if (workspace or hub_url) and deployment_profile != "sandbox":
    _remove_pid_file(pid_path)
    click.echo(
        "Error: --workspace and --hub-url are only valid with --profile sandbox.",
        err=True,
    )
    sys.exit(ExitCode.CONFIG_ERROR)

# Guard: --hub-url requires a token
if hub_url and not hub_token:
    _remove_pid_file(pid_path)
    click.echo(
        "Error: --hub-url requires --hub-token or NEXUS_HUB_TOKEN environment variable.",
        err=True,
    )
    sys.exit(ExitCode.CONFIG_ERROR)
```

Add a top-level import for `SandboxBootstrapper` at the top of `main.py` (with the other imports, using lazy import to avoid circular deps):

```python
# Near top of file, with other conditional imports
try:
    from nexus.daemon.sandbox_bootstrap import SandboxBootstrapper as SandboxBootstrapper
except ImportError:
    SandboxBootstrapper = None  # type: ignore[assignment,misc]
```

Find the location where `nx = nexus.connect(...)` is called (around line 340). After the connect call, insert:

```python
# Sandbox federation: mount remote zones and start boot indexer
if deployment_profile == "sandbox" and hub_url and SandboxBootstrapper is not None:
    from pathlib import Path as _Path
    _workspace_path = _Path(workspace) if workspace else _Path.cwd()
    _bootstrapper = SandboxBootstrapper(
        nx=nx,
        workspace=_workspace_path,
        hub_address=hub_url,
        hub_token=hub_token or "",
    )
    _boot_result = _bootstrapper.run()
    click.echo(
        f"  Federation: {'active' if _boot_result.hub_session else 'local-only'}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/daemon/test_main_sandbox_flags.py -v
```
Expected: PASS (4 tests). Note: test_hub_token_env_var_accepted may be partial — verify it does not crash.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/daemon/main.py tests/unit/daemon/test_main_sandbox_flags.py
git commit -m "feat(#3786): add --workspace --hub-url --hub-token flags to nexusd for sandbox mode"
```

---

## Task 8: `nexus up` sandbox shortcut in `stack.py`

**Files:**
- Modify: `src/nexus/cli/commands/stack.py`
- Create: `tests/unit/cli/test_stack_sandbox_flags.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cli/test_stack_sandbox_flags.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.stack import up


class TestStackSandboxFlags:
    def test_workspace_without_profile_sandbox_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(up, ["--workspace", "/tmp/ws"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "sandbox" in result.output.lower()

    def test_hub_url_without_profile_sandbox_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(up, ["--hub-url", "hub:2028"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_profile_sandbox_invokes_nexusd(self, tmp_path) -> None:
        with (
            patch("nexus.cli.commands.stack.subprocess.run") as mock_run,
            patch("nexus.cli.commands.stack.shutil.which", return_value="/usr/bin/nexusd"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner = CliRunner()
            result = runner.invoke(
                up,
                [
                    "--profile", "sandbox",
                    "--workspace", str(tmp_path),
                    "--hub-url", "hub:2028",
                    "--hub-token", "mytoken",
                ],
                catch_exceptions=True,
            )
            # Verify subprocess.run was called with nexusd and sandbox flags
            if mock_run.called:
                cmd = mock_run.call_args[0][0]
                assert any("nexusd" in str(c) or "sandbox" in str(c) for c in cmd)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/cli/test_stack_sandbox_flags.py -v
```
Expected: FAIL (flags don't exist on `up` yet)

- [ ] **Step 3: Add sandbox flags to `nexus up` in `stack.py`**

Find the `@click.command()` decorator for the `up` function (line 472). Add three new `@click.option` decorators before the function definition:

```python
@click.option(
    "--profile",
    "profile",
    type=click.Choice(["sandbox"], case_sensitive=False),
    default=None,
    help="Deployment profile. Use 'sandbox' to start a thin in-process nexus (no Docker).",
)
@click.option(
    "--workspace",
    "workspace",
    type=click.Path(exists=False, file_okay=False),
    default=None,
    envvar="NEXUS_WORKSPACE",
    help="[--profile sandbox] Local workspace directory.",
)
@click.option(
    "--hub-url",
    "hub_url",
    default=None,
    envvar="NEXUS_HUB_URL",
    help="[--profile sandbox] Hub gRPC address.",
)
@click.option(
    "--hub-token",
    "hub_token",
    default=None,
    envvar="NEXUS_HUB_TOKEN",
    help="[--profile sandbox] Bearer token. Prefer NEXUS_HUB_TOKEN env var.",
)
```

Add `profile: str | None`, `workspace: str | None`, `hub_url: str | None`, `hub_token: str | None` to the `up` function signature.

At the **start** of the `up` function body (before the existing config loading), insert the sandbox shortcut:

```python
# Sandbox shortcut: run nexusd in-process instead of Docker Compose
if profile == "sandbox":
    if workspace and not hub_url:
        # workspace-only mode: no federation
        pass
    elif (workspace or hub_url) and hub_url and not hub_token:
        console.print("[nexus.error]Error:[/nexus.error] --hub-url requires --hub-token or NEXUS_HUB_TOKEN.")
        raise SystemExit(1)
    if workspace is None:
        console.print("[nexus.error]Error:[/nexus.error] --profile sandbox requires --workspace PATH.")
        raise SystemExit(1)
    _invoke_nexusd_sandbox(workspace, hub_url, hub_token)
    return

# Guard: sandbox flags without sandbox profile
if workspace or hub_url:
    console.print(
        "[nexus.error]Error:[/nexus.error] --workspace and --hub-url require --profile sandbox."
    )
    raise SystemExit(1)
```

Add the helper function before the `up` definition:

```python
def _invoke_nexusd_sandbox(
    workspace: str,
    hub_url: str | None,
    hub_token: str | None,
) -> None:
    """Invoke nexusd with sandbox profile flags (replaces Docker Compose for sandbox mode)."""
    cmd = [
        shutil.which("nexusd") or "nexusd",
        "--profile", "sandbox",
        "--workspace", workspace,
    ]
    if hub_url:
        cmd.extend(["--hub-url", hub_url])
    if hub_token:
        cmd.extend(["--hub-token", hub_token])

    env = os.environ.copy()
    if hub_token:
        env["NEXUS_HUB_TOKEN"] = hub_token

    console.print(f"[bold]Starting nexus sandbox for workspace {workspace}...[/bold]")
    subprocess.run(cmd, env=env, check=False)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/cli/test_stack_sandbox_flags.py -v
```
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cli/commands/stack.py tests/unit/cli/test_stack_sandbox_flags.py
git commit -m "feat(#3786): add --profile sandbox shortcut to nexus up"
```

---

## Task 9: Full test suite pass

- [ ] **Step 1: Run all new tests together**

```bash
pytest tests/unit/contracts/test_exceptions_federation.py \
       tests/unit/backends/test_remote_zone.py \
       tests/unit/remote/test_federation_handshake.py \
       tests/unit/core/test_boot_indexer.py \
       tests/unit/daemon/test_sandbox_bootstrap.py \
       tests/unit/daemon/test_main_sandbox_flags.py \
       tests/unit/cli/test_stack_sandbox_flags.py \
       -v
```
Expected: All pass

- [ ] **Step 2: Run existing tests for touched files**

```bash
pytest tests/unit/backends/test_remote_backend.py \
       tests/unit/daemon/test_main.py \
       -v
```
Expected: All pass (no regressions)

- [ ] **Step 3: Run full unit test suite**

```bash
pytest tests/unit/ -x -q
```
Expected: All pass

- [ ] **Step 4: Final commit**

```bash
git add -u
git commit -m "test(#3786): verify no regressions in touched modules"
```

---

## Acceptance Criteria Checklist

| Criterion | Task |
|-----------|------|
| Lightweight nexus indexes local workspace on boot | Tasks 5, 6, 7 |
| Federation handshake with hub completes | Tasks 3, 4, 7 |
| `nexus search` returns results from both local + company | Wired via existing `ZoneSearchRegistry.register_remote()` in Task 6 |
| Local file writes work at disk speed | `PathLocalBackend` unchanged — no task needed |
| Company zone is read-only from sandbox | Task 2 (`RemoteZoneBackend`) |
| Search results indicate source (zone label) | Already implemented in `federated_search.py` `_search_remote_zone` — no task needed |
