"""FileWatcher — kernel file change notification (inotify equivalent).

Kernel primitive (§4.3) providing file change notification with two paths:

    Local (kernel-owned):
        ``on_mutation()`` registered as VFSObserver on KernelDispatch.
        Resolves pending waiters via in-memory asyncio.Future (~0µs).

    Remote (kernel-knows):
        Optional ``RemoteWatchProtocol`` for distributed watch across nodes.
        Set via ``set_remote_watcher()`` by whoever constructs the distributed
        infra (e.g. federation service). When None, ``wait()`` is local-only.

Linux analogue: ``inotify(7)`` for local notification. No Linux equivalent
for the remote path — that's a federation extension.

    file_watcher.py  = inotify (kernel file change notification)
    file_events.py   = fsnotify_event (immutable mutation records)

See: KERNEL-ARCHITECTURE.md §4.3, §4.5
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import threading
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nexus.core.file_events import ALL_FILE_EVENTS

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.core.file_events import FileEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RemoteWatchProtocol — kernel-agnostic interface for distributed watch
# ---------------------------------------------------------------------------


@runtime_checkable
class RemoteWatchProtocol(Protocol):
    """Kernel-agnostic interface for remote file change notification.

    Any implementation that provides ``wait_for_event()`` satisfies this
    protocol. The kernel does not know whether it's backed by NATS, Redis,
    or any other transport — implementation-agnostic by design.
    """

    async def wait_for_event(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_version: int | None = None,
    ) -> "FileEvent | None": ...


# ---------------------------------------------------------------------------
# Internal waiter dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Waiter:
    """Internal waiter for OBSERVE-path event delivery."""

    path_pattern: str
    future: asyncio.Future["FileEvent"]
    loop: asyncio.AbstractEventLoop


# ---------------------------------------------------------------------------
# FileWatcher — kernel primitive
# ---------------------------------------------------------------------------


class FileWatcher:
    """Kernel file change notification (inotify equivalent).

    Kernel-owned: local OBSERVE-path waiters (in-memory futures).
    Kernel-knows: optional ``RemoteWatchProtocol`` for distributed watch.

    Created in ``NexusFS.__init__()`` alongside PipeManager/StreamManager.
    Registered as VFSObserver via ``hook_spec()`` at enlist time.
    """

    event_mask: int = ALL_FILE_EVENTS  # ObserverRegistry bitmask

    def __init__(self) -> None:
        self._waiters: list[_Waiter] = []
        self._waiters_lock = threading.Lock()
        self._remote_watcher: RemoteWatchProtocol | None = None

    # ------------------------------------------------------------------
    # Kernel-knows: remote watcher setter
    # ------------------------------------------------------------------

    def set_remote_watcher(self, watcher: RemoteWatchProtocol) -> None:
        """Set the remote watcher for distributed file change notification.

        Called by whoever constructs the distributed infra (e.g. federation
        service). First consumer that needs distributed events constructs
        the implementation and calls this.
        """
        self._remote_watcher = watcher
        logger.info("FileWatcher: remote watcher set (%s)", type(watcher).__name__)

    @property
    def has_remote_watcher(self) -> bool:
        """Whether a remote watcher is configured."""
        return self._remote_watcher is not None

    # ------------------------------------------------------------------
    # Hook spec — register as VFSObserver
    # ------------------------------------------------------------------

    def hook_spec(self) -> "HookSpec":
        """Declare VFS hooks: FileWatcher registers as an OBSERVE observer."""
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(observers=(self,))

    # ------------------------------------------------------------------
    # VFSObserver: on_mutation (OBSERVE phase)
    # ------------------------------------------------------------------

    def on_mutation(self, event: "FileEvent") -> None:
        """Called by KernelDispatch.notify() on every local mutation.

        Matches the event against pending waiters and resolves their futures.
        Pure in-memory (~0μs) — no I/O, no await needed.
        """
        with self._waiters_lock:
            for w in self._waiters:
                if not w.future.done() and event.matches_path_pattern(w.path_pattern):
                    w.future.set_result(event)

    # ------------------------------------------------------------------
    # Local wait (kernel-owned)
    # ------------------------------------------------------------------

    async def wait_local(
        self,
        path: str,
        timeout: float,
    ) -> "FileEvent | None":
        """Wait for a local mutation via OBSERVE-path future (~0µs).

        Creates an asyncio.Future, registers it as a waiter, and blocks
        until ``on_mutation()`` resolves it or timeout expires.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future["FileEvent"] = loop.create_future()
        waiter = _Waiter(path_pattern=path, future=future, loop=loop)

        with self._waiters_lock:
            self._waiters.append(waiter)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return None
        finally:
            with self._waiters_lock, contextlib.suppress(ValueError):
                self._waiters.remove(waiter)

    # ------------------------------------------------------------------
    # Remote wait (kernel-knows, optional)
    # ------------------------------------------------------------------

    async def _wait_remote(
        self,
        zone_id: str,
        path: str,
        timeout: float,
    ) -> "FileEvent | None":
        """Wait for a remote mutation via RemoteWatchProtocol."""
        if self._remote_watcher is None:
            return None
        return await self._remote_watcher.wait_for_event(
            zone_id=zone_id,
            path_pattern=path,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Unified wait — races local + remote
    # ------------------------------------------------------------------

    async def wait(
        self,
        path: str,
        timeout: float = 30.0,
        zone_id: str = "root",
    ) -> "FileEvent | None":
        """Wait for file changes — races local OBSERVE + remote watcher.

        Local OBSERVE covers same-node mutations (~0µs). Remote watcher
        covers intra-zone inter-node mutations (e.g. Raft follower apply
        gap via WALStreamBackend). When both available, races them.

        Cross-zone watch is NOT handled here — zones are visibility
        boundaries. Cross-zone access goes through DT_MOUNT at the
        routing layer (DriverLifecycleCoordinator).
        """
        has_remote = self._remote_watcher is not None

        if has_remote:
            task_local = asyncio.create_task(self.wait_local(path, timeout))
            task_remote = asyncio.create_task(self._wait_remote(zone_id, path, timeout))

            done, pending = await asyncio.wait(
                {task_local, task_remote},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

            return done.pop().result()

        return await self.wait_local(path, timeout)
