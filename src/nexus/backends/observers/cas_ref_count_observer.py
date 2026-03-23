"""CAS ref_count observer — decrements ref_count on write-overwrite and delete.

Registered in KernelDispatch OBSERVE phase via CASAddressingEngine.hook_spec().
Calls engine.release_content() which only decrements ref_count — physical
cleanup is deferred to CASGarbageCollector.

Issue #1320: CAS async GC.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
    from nexus.core.file_events import FileEvent

logger = logging.getLogger(__name__)


class CASRefCountObserver:
    """OBSERVE-phase observer that decrements CAS ref_count on mutations.

    - FILE_WRITE with old_etag != new etag: release old content
    - FILE_DELETE with etag: release deleted content

    Must not raise — KernelDispatch catches and logs observer exceptions.
    """

    def __init__(self, engine: CASAddressingEngine) -> None:
        self._engine = engine

    def on_mutation(self, event: FileEvent) -> None:
        from nexus.core.file_events import FileEventType

        if event.type == FileEventType.FILE_WRITE:
            old_etag = getattr(event, "old_etag", None)
            if old_etag and old_etag != event.etag:
                try:
                    self._engine.release_content(old_etag)
                except Exception:
                    logger.warning(
                        "CASRefCountObserver: failed to release old_etag %s for %s",
                        old_etag[:16],
                        event.path,
                        exc_info=True,
                    )

        elif event.type == FileEventType.FILE_DELETE:
            if event.etag:
                try:
                    self._engine.release_content(event.etag)
                except Exception:
                    logger.warning(
                        "CASRefCountObserver: failed to release etag %s for %s",
                        event.etag[:16],
                        event.path,
                        exc_info=True,
                    )
