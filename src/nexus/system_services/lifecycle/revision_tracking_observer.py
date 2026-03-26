"""RevisionTrackingObserver — VFSObserver that feeds RevisionNotifier (Issue #1382).

Registered in KernelDispatch OBSERVE phase. On every write/delete/rename
mutation that carries a version, calls ``RevisionNotifier.notify_revision()``
so that consistency-token waiters (Issue #1180) and reactive subscriptions
are unblocked.

This replaces the old kernel-internal ``_increment_vfs_revision()`` that was
deleted in the decomposition (#899).  The observer pattern moves revision
tracking from the kernel into the service layer where it belongs.

Issue #1748: async on_mutation + event_mask filtering.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.core.file_events import ALL_FILE_EVENTS

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.core.file_events import FileEvent
    from nexus.lib.revision_notifier import RevisionNotifierBase

logger = logging.getLogger(__name__)


class RevisionTrackingObserver:
    """Track per-zone revisions via KernelDispatch OBSERVE phase.

    Implements the ``VFSObserver`` protocol (single method: ``on_mutation``).
    Receives frozen ``FileEvent`` after every successful VFS mutation.

    Only events with a non-None ``version`` and ``zone_id`` are tracked.
    Directory events (mkdir/rmdir) typically have no version and are skipped.
    """

    __slots__ = ("_notifier",)

    event_mask: int = ALL_FILE_EVENTS

    # ── Hook spec (duck-typed) (Issue #1616) ──────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(observers=(self,))

    def __init__(self, revision_notifier: RevisionNotifierBase) -> None:
        self._notifier = revision_notifier

    async def on_mutation(self, event: FileEvent) -> None:
        """Update revision tracking when a versioned mutation occurs."""
        version = event.version
        zone_id = event.zone_id
        if version is not None and zone_id is not None:
            self._notifier.notify_revision(zone_id, version)
