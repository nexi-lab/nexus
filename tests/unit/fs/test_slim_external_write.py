"""Regression tests: slim-mode ExternalRouteResult write + delete path.

Connector mounts (gmail, calendar, gdrive, …) use ``DT_EXTERNAL_STORAGE``
which the router resolves to ``ExternalRouteResult``.  The kernel's
write path for those routes is just ``backend.write_content`` +
``metastore.put`` — wrapped in ``dispatch_pre_hooks`` / ``dispatch_post_hooks``
calls that the slim package has no Rust kernel for.  Without this
fall-through, every connector write in the slim package raised
``AttributeError: 'NoneType' object has no attribute 'dispatch_pre_hooks'``.

The fix is connector-agnostic: it resolves the route, checks for
``ExternalRouteResult``, and calls whatever ``write_content`` /
``delete_content`` the connector provides.  Works for any connector
that subclasses ``PathAddressingEngine`` (gmail, calendar, gdrive,
onedrive, sharepoint, slack, …) without per-connector facade changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.metadata import DT_EXTERNAL_STORAGE
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.core.object_store import WriteResult
from nexus.fs import _make_mount_entry
from nexus.fs._facade import SlimNexusFS
from nexus.fs._sqlite_meta import SQLiteMetastore


class _FakeRouteResult:
    """Minimal route result returned by the mock _py_kernel.route()."""

    def __init__(self, mount_point: str, backend_path: str) -> None:
        self.mount_point = mount_point
        self.backend_path = backend_path


def _make_mock_py_kernel(mount_point: str, zone_id: str = ROOT_ZONE_ID) -> MagicMock:
    """Build a mock Rust kernel whose ``route()`` performs prefix-match routing.

    The real Rust kernel is unavailable in the slim test environment, but after
    the Python LPM fallback was deleted, ``_resolve_external_route`` requires
    ``_py_kernel.route()`` to succeed.  This mock replicates just enough
    behaviour to let the facade route paths under *mount_point*.
    """
    canonical = f"/{zone_id}{mount_point}"  # e.g. "/root/ext"
    mk = MagicMock()

    def _route(path: str, _zone_id: str = zone_id) -> _FakeRouteResult:
        # Strip leading mount_point prefix to derive backend_path
        if path.startswith(mount_point + "/"):
            bp = path[len(mount_point) + 1 :]
        elif path == mount_point:
            bp = ""
        else:
            from nexus.contracts.exceptions import PathNotMountedError

            raise PathNotMountedError(path)
        return _FakeRouteResult(mount_point=canonical, backend_path=bp)

    mk.route = _route
    return mk


class _FakeExternalBackend:
    """Minimal connector-style backend — records writes and deletes so
    the test can assert the slim facade hit the backend directly
    (instead of going through the kernel-hook path)."""

    name = "fake_external"
    has_root_path = False

    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []
        self.deletes: list[str] = []
        self.store: dict[str, bytes] = {}

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> WriteResult:
        assert context is not None
        assert context.backend_path, "facade must populate backend_path"
        self.writes.append((context.backend_path, bytes(content)))
        self.store[context.backend_path] = bytes(content)
        return WriteResult(
            content_id=f"content-{len(self.writes)}",
            version=f"v{len(self.writes)}",
            size=len(content),
        )

    def read_content(self, content_id: str, context: OperationContext | None = None) -> bytes:
        assert context is not None and context.backend_path
        if context.backend_path not in self.store:
            raise NexusFileNotFoundError(context.backend_path)
        return self.store[context.backend_path]

    def delete_content(self, content_id: str, context: OperationContext | None = None) -> None:
        assert context is not None and context.backend_path
        self.deletes.append(context.backend_path)
        self.store.pop(context.backend_path, None)

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        return []


def _build_slim_with_external_mount(
    tmp_path: Path,
) -> tuple[SlimNexusFS, _FakeExternalBackend]:
    metastore = SQLiteMetastore(str(tmp_path / "meta.db"))
    backend = _FakeExternalBackend()
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        init_cred=OperationContext(
            user_id="slim-ext", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True
        ),
    )
    mount_point = "/ext"
    # DLC stores the mount info; the metastore entry marks it
    # DT_EXTERNAL_STORAGE so the facade's route() returns an
    # ExternalRouteResult.  Matches the production flow.
    kernel._driver_coordinator._store_mount_info(mount_point, backend, is_external=True)
    metastore.put(_make_mount_entry(mount_point, backend.name, entry_type=DT_EXTERNAL_STORAGE))
    # Force slim-mode (_is_slim_mode checks NexusFS._kernel is None) while
    # giving the DLC a mock Rust kernel for DLC.resolve_path() routing.
    # The Python LPM fallback was deleted; DLC.resolve_path() is the only
    # routing path now.
    kernel._driver_coordinator._kernel = _make_mock_py_kernel(mount_point)
    kernel._kernel = None
    return SlimNexusFS(kernel), backend


def test_slim_write_reaches_connector_backend(tmp_path: Path) -> None:
    """Slim mode + external route: facade.write must hit
    backend.write_content directly (not crash on dispatch_pre_hooks)."""
    slim, backend = _build_slim_with_external_mount(tmp_path)
    try:
        result = slim.write("/ext/SEND/_new.yaml", b"to: foo\nbody: hi")
        assert result["path"] == "/ext/SEND/_new.yaml"
        assert result["size"] == len(b"to: foo\nbody: hi")
        assert result["etag"]
        # Backend saw the right backend_path
        assert backend.writes == [("SEND/_new.yaml", b"to: foo\nbody: hi")]
        # Metadata persisted so subsequent stat() sees the file.
        meta = slim._kernel.metadata.get("/ext/SEND/_new.yaml")
        assert meta is not None
        assert meta.size == len(b"to: foo\nbody: hi")
    finally:
        slim.close()


def test_slim_write_second_time_increments_version(tmp_path: Path) -> None:
    slim, backend = _build_slim_with_external_mount(tmp_path)
    try:
        slim.write("/ext/a.yaml", b"one")
        r2 = slim.write("/ext/a.yaml", b"two")
        assert r2["version"] == 2
        assert backend.writes[-1] == ("a.yaml", b"two")
    finally:
        slim.close()


def test_slim_rewrite_does_not_forward_stale_content_id(tmp_path: Path) -> None:
    """Regression: a rewrite of the same virtual path must NOT pass the
    prior ``content_id`` (physical_path) back into ``write_content``.

    Path-addressed connectors treat ``content_id`` as an override for
    ``context.backend_path`` on splice writes (offset>0).  Forwarding
    the last write's id on a full overwrite can misroute the write to
    a different key shape on some backends.  The kernel only forwards
    ``physical_path`` when ``offset > 0`` (NexusFS._write_content); the
    slim facade must match that discipline — full overwrites always
    send an empty ``content_id``.
    """

    captured: list[dict[str, Any]] = []

    class _CapturingBackend(_FakeExternalBackend):
        def write_content(
            self,
            content: bytes,
            content_id: str = "",
            *,
            offset: int = 0,
            context: OperationContext | None = None,
        ) -> WriteResult:
            captured.append({"content_id": content_id, "offset": offset})
            return super().write_content(content, content_id, offset=offset, context=context)

    backend = _CapturingBackend()
    metastore = SQLiteMetastore(str(tmp_path / "m.db"))
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        init_cred=OperationContext(user_id="u", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True),
    )
    kernel._driver_coordinator._store_mount_info("/ext", backend, is_external=True)
    metastore.put(_make_mount_entry("/ext", backend.name, entry_type=DT_EXTERNAL_STORAGE))
    kernel._driver_coordinator._kernel = _make_mock_py_kernel("/ext")
    kernel._kernel = None
    slim = SlimNexusFS(kernel)

    try:
        slim.write("/ext/rewrite.yaml", b"first")
        slim.write("/ext/rewrite.yaml", b"second")
        assert len(captured) == 2
        assert captured[0] == {"content_id": "", "offset": 0}, captured
        # Critical: rewrite must also send empty content_id even though
        # existing metadata now carries a physical_path from write #1.
        assert captured[1] == {"content_id": "", "offset": 0}, captured
    finally:
        slim.close()


def test_slim_delete_reaches_connector_backend(tmp_path: Path) -> None:
    slim, backend = _build_slim_with_external_mount(tmp_path)
    try:
        slim.write("/ext/to-delete.yaml", b"payload")
        assert backend.store.get("to-delete.yaml") == b"payload"

        slim.delete("/ext/to-delete.yaml")
        assert backend.deletes == ["to-delete.yaml"]
        assert slim._kernel.metadata.get("/ext/to-delete.yaml") is None
    finally:
        slim.close()


def test_slim_external_write_is_connector_agnostic(tmp_path: Path) -> None:
    """The fall-through checks only for ExternalRouteResult + a
    write_content method — no per-connector logic.  Swap in a backend
    with an arbitrary name and the same code path succeeds."""
    metastore = SQLiteMetastore(str(tmp_path / "m.db"))
    backend = _FakeExternalBackend()
    backend.name = "some_totally_different_connector"
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        init_cred=OperationContext(user_id="u", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True),
    )
    kernel._driver_coordinator._store_mount_info("/any", backend, is_external=True)
    metastore.put(_make_mount_entry("/any", backend.name, entry_type=DT_EXTERNAL_STORAGE))
    kernel._driver_coordinator._kernel = _make_mock_py_kernel("/any")
    kernel._kernel = None
    slim = SlimNexusFS(kernel)
    try:
        result = slim.write("/any/file.yaml", b"data")
        assert result["size"] == 4
        assert backend.writes == [("file.yaml", b"data")]
    finally:
        slim.close()


def test_slim_write_is_atomic_under_concurrency(tmp_path: Path) -> None:
    """Two concurrent writers to the same path must leave metadata with
    monotonic version — no two writes collapsing into version=1."""
    import threading

    slim, backend = _build_slim_with_external_mount(tmp_path)

    def _worker(payload: bytes) -> None:
        slim.write("/ext/race.yaml", payload)

    try:
        threads = [
            threading.Thread(target=_worker, args=(f"payload-{i}".encode(),)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        meta = slim._kernel.metadata.get("/ext/race.yaml")
        assert meta is not None
        # 8 serialized writes → final version must be 8.  A non-atomic
        # path would collapse into version < 8 because writers both
        # read the same pre-write version and then both persist N+1.
        assert meta.version == 8, (
            f"concurrent writes lost version monotonicity — final version={meta.version}"
        )
        assert len(backend.writes) == 8
    finally:
        slim.close()


def test_slim_write_lock_is_shared_across_facade_instances(tmp_path: Path) -> None:
    """Two SlimNexusFS wrappers around the same NexusFS must serialize
    writes to the same path — if they kept per-instance lock pools,
    concurrent writers via different wrappers could both read version
    N and both persist N+1 (lost version monotonicity)."""
    import threading

    metastore = SQLiteMetastore(str(tmp_path / "m.db"))
    backend = _FakeExternalBackend()
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        init_cred=OperationContext(user_id="u", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True),
    )
    kernel._driver_coordinator._store_mount_info("/ext", backend, is_external=True)
    metastore.put(_make_mount_entry("/ext", backend.name, entry_type=DT_EXTERNAL_STORAGE))
    kernel._driver_coordinator._kernel = _make_mock_py_kernel("/ext")
    kernel._kernel = None

    # Two wrappers around the same kernel — must share lock pool so
    # writes serialize across them.
    slim_a = SlimNexusFS(kernel)
    slim_b = SlimNexusFS(kernel)
    assert slim_a._slim_lock_pool is slim_b._slim_lock_pool, (
        "facade instances wrapping the same kernel must share the lock pool"
    )

    def _work(slim: SlimNexusFS, payload: bytes) -> None:
        slim.write("/ext/shared.yaml", payload)

    threads: list[threading.Thread] = []
    for i in range(8):
        slim = slim_a if i % 2 == 0 else slim_b
        threads.append(threading.Thread(target=_work, args=(slim, f"msg-{i}".encode())))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    meta = kernel.metadata.get("/ext/shared.yaml")
    assert meta is not None
    assert meta.version == 8, f"cross-wrapper writes lost monotonicity — version={meta.version}"
    slim_a.close()


def test_slim_external_write_not_triggered_for_non_external_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The external-write fall-through must NOT swallow writes to CAS
    or path-local mounts — those paths should still hit the kernel
    (and surface the real `_kernel=None` error in the slim test env
    until the kernel gains its own slim-safe write path)."""
    from nexus.backends.storage.path_local import PathLocalBackend

    metastore = SQLiteMetastore(str(tmp_path / "m.db"))
    data = tmp_path / "data"
    data.mkdir()
    backend = PathLocalBackend(root_path=data)
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        init_cred=OperationContext(user_id="u", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True),
    )
    kernel._driver_coordinator._store_mount_info("/local", backend)
    # Note: NOT DT_EXTERNAL_STORAGE → _resolve_external_route returns None
    # because the is_external check fails.  The fall-through short-circuits
    # and the kernel write path runs (which raises AttributeError in slim mode).
    metastore.put(_make_mount_entry("/local", backend.name))
    kernel._driver_coordinator._kernel = _make_mock_py_kernel("/local")
    kernel._kernel = None  # force slim-mode so kernel.write raises AttributeError
    slim = SlimNexusFS(kernel)

    external_called: list[Any] = []
    orig = slim._try_external_write

    def _spy(path: str, content: bytes) -> dict[str, Any] | None:
        external_called.append(path)
        return orig(path, content)

    monkeypatch.setattr(slim, "_try_external_write", _spy)

    with pytest.raises(AttributeError):
        slim.write("/local/file.txt", b"x")
    # Confirms the spy was called AND returned None (no swallow).
    assert external_called == ["/local/file.txt"]
    slim.close()
