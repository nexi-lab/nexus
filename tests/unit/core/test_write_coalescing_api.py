from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _Kernel:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str | None]] = []

    def flush_write_buffer(self, path: str | None = None, zone_id: str | None = None) -> object:
        self.calls.append((path, zone_id))
        return SimpleNamespace(flushed=1, failed=0, errors=[])


def test_flush_write_buffer_forwards_to_kernel() -> None:
    from nexus.core.nexus_fs_content import ContentMixin
    from nexus.core.nexus_fs_internal import InternalMixin

    class FS(ContentMixin, InternalMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()
            self._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)

    fs = FS()
    result = fs.flush_write_buffer("/workspace/a.txt")

    assert result == {"flushed": 1, "failed": 0, "errors": []}
    assert fs._kernel.calls == [("/workspace/a.txt", "root")]


def test_close_flushes_after_close_callbacks_before_release_metastores() -> None:
    from nexus.core.nexus_fs import NexusFS

    calls: list[str] = []

    class Kernel:
        def flush_write_buffer(self, path=None, zone_id=None):
            calls.append("flush")
            return SimpleNamespace(flushed=1, failed=0, errors=[])

        def close_all_pipes(self):
            calls.append("pipes")

        def close_all_streams(self):
            calls.append("streams")

        def service_close_all(self):
            calls.append("services")

        def release_metastores(self):
            calls.append("metastores")

    fs = object.__new__(NexusFS)
    fs._kernel = Kernel()
    fs._zone_id = "root"
    fs._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)
    fs._close_callbacks = [lambda: calls.append("callback")]
    fs._transport_pool = None
    fs._record_store = None
    fs._runtime_closeables = []

    NexusFS.close(fs)

    assert calls.index("callback") < calls.index("flush")
    assert calls.index("services") < calls.index("flush")
    assert calls.index("flush") < calls.index("metastores")


def test_close_propagates_final_flush_failure() -> None:
    from nexus.core.nexus_fs import NexusFS

    calls: list[str] = []

    class Kernel:
        def flush_write_buffer(self, path=None, zone_id=None):
            calls.append("flush")
            raise RuntimeError("flush failed")

        def close_all_pipes(self):
            calls.append("pipes")
            raise RuntimeError("pipe close failed")

        def close_all_streams(self):
            calls.append("streams")

        def service_close_all(self):
            calls.append("services")

        def release_metastores(self):
            calls.append("metastores")

    class TransportPool:
        def close_all(self):
            calls.append("transport")

    class RecordStore:
        def close(self):
            calls.append("record_store")

    class RuntimeCloseable:
        def close(self):
            calls.append("runtime")

    fs = object.__new__(NexusFS)
    fs._kernel = Kernel()
    fs._zone_id = "root"
    fs._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)
    fs._close_callbacks = [lambda: calls.append("callback")]
    fs._transport_pool = TransportPool()
    fs._record_store = RecordStore()
    fs._runtime_closeables = [RuntimeCloseable()]

    with pytest.raises(RuntimeError, match="flush failed"):
        NexusFS.close(fs)

    assert calls.index("callback") < calls.index("flush")
    assert calls.index("services") < calls.index("flush")
    assert "pipes" in calls
    assert "streams" in calls
    assert "transport" in calls
    assert "record_store" in calls
    assert "runtime" in calls
    assert "metastores" not in calls


def test_fsync_and_sync_forward_to_flush() -> None:
    from nexus.core.nexus_fs_content import ContentMixin
    from nexus.core.nexus_fs_internal import InternalMixin

    class FS(ContentMixin, InternalMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()
            self._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)

    fs = FS()
    assert fs.fsync("/workspace/a.txt") == {"flushed": 1, "failed": 0, "errors": []}
    assert fs.sync() == {"flushed": 1, "failed": 0, "errors": []}
    assert fs._kernel.calls == [("/workspace/a.txt", "root"), (None, "root")]


def test_sync_scopes_non_root_rpc_context_to_caller_zone_prefix() -> None:
    from nexus.core.nexus_fs_content import ContentMixin
    from nexus.core.nexus_fs_internal import InternalMixin
    from nexus.server._kernel_syscall_dispatch import dispatch_kernel_syscall

    class FS(ContentMixin, InternalMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()
            self._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)

    fs = FS()
    context = SimpleNamespace(zone_id="tenant-a", agent_id="agent-1", is_admin=False)

    result = asyncio.run(dispatch_kernel_syscall(fs, "sync", {}, context))

    assert result == {"flushed": 1, "failed": 0, "errors": []}
    assert fs._kernel.calls == [("/zone/tenant-a", "root")]


def test_flush_write_buffer_scopes_non_root_path_to_caller_zone_prefix() -> None:
    from nexus.core.nexus_fs_content import ContentMixin
    from nexus.core.nexus_fs_internal import InternalMixin

    class FS(ContentMixin, InternalMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()
            self._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)

    fs = FS()
    context = SimpleNamespace(zone_id="tenant-a", agent_id="agent-1", is_admin=False)

    result = fs.flush_write_buffer("/workspace/a.txt", context=context)

    assert result == {"flushed": 1, "failed": 0, "errors": []}
    assert fs._kernel.calls == [("/zone/tenant-a/workspace/a.txt", "root")]


def test_sync_requires_explicit_zone_for_multi_zone_context() -> None:
    from nexus.contracts.exceptions import AccessDeniedError
    from nexus.core.nexus_fs_content import ContentMixin
    from nexus.core.nexus_fs_internal import InternalMixin

    class FS(ContentMixin, InternalMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()
            self._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)

    fs = FS()
    context = SimpleNamespace(
        zone_id="root",
        agent_id="agent-1",
        is_admin=False,
        zone_perms=(("eng", "rw"), ("ops", "rw")),
    )

    with pytest.raises(AccessDeniedError):
        fs.sync(context=context)

    assert fs._kernel.calls == []


def test_sync_scopes_allowed_multi_zone_request() -> None:
    from nexus.core.nexus_fs_content import ContentMixin
    from nexus.core.nexus_fs_internal import InternalMixin

    class FS(ContentMixin, InternalMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()
            self._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)

    fs = FS()
    context = SimpleNamespace(
        zone_id="root",
        agent_id="agent-1",
        is_admin=False,
        zone_perms=(("eng", "rw"), ("ops", "r")),
    )

    result = fs.sync(zone_id="eng", context=context)

    assert result == {"flushed": 1, "failed": 0, "errors": []}
    assert fs._kernel.calls == [("/zone/eng", "root")]


def test_flush_write_buffer_rejects_read_only_multi_zone_path() -> None:
    from nexus.contracts.exceptions import AccessDeniedError
    from nexus.core.nexus_fs_content import ContentMixin
    from nexus.core.nexus_fs_internal import InternalMixin

    class FS(ContentMixin, InternalMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()
            self._init_cred = SimpleNamespace(zone_id="root", agent_id=None, is_admin=True)

    fs = FS()
    context = SimpleNamespace(
        zone_id="root",
        agent_id="agent-1",
        is_admin=False,
        zone_perms=(("eng", "rw"), ("ops", "r")),
    )

    with pytest.raises(AccessDeniedError):
        fs.flush_write_buffer("/zone/ops/workspace/a.txt", context=context)

    assert fs._kernel.calls == []
