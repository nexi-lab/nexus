"""Async wrapper for NamespaceManager (Issue #1440).

Thin adapter that wraps the sync ``NamespaceManager`` to satisfy
``NamespaceManagerProtocol`` (all-async signatures).  Uses
``asyncio.to_thread`` for methods that may trigger ReBAC rebuilds.

References:
    - Issue #1440: Async wrappers for 4 sync kernel protocols
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nexus.services.protocols.namespace_manager import NamespaceMount

if TYPE_CHECKING:
    from nexus.services.permissions.namespace_manager import MountEntry, NamespaceManager


def _to_namespace_mount(
    entry: MountEntry,
    subject: tuple[str, str],
    zone_id: str | None,
) -> NamespaceMount:
    """Convert a ``MountEntry`` to the protocol-level ``NamespaceMount``.

    ``MountEntry`` only carries ``virtual_path``; the wrapper enriches it
    with subject identity and zone context from the call site.
    """
    return NamespaceMount(
        virtual_path=entry.virtual_path,
        subject_type=subject[0],
        subject_id=subject[1],
        zone_id=zone_id,
    )


class AsyncNamespaceManager:
    """Async adapter for ``NamespaceManager`` conforming to ``NamespaceManagerProtocol``.

    ``is_visible`` and ``get_mount_table`` may trigger ReBAC rebuilds (I/O),
    so they delegate via ``asyncio.to_thread``.  ``invalidate`` also uses
    ``to_thread`` for consistency (cache operations under lock).
    """

    def __init__(self, inner: NamespaceManager) -> None:
        self._inner = inner

    async def is_visible(
        self,
        subject: tuple[str, str],
        path: str,
        *,
        zone_id: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._inner.is_visible,
            subject,
            path,
            zone_id=zone_id,
        )

    async def get_mount_table(
        self,
        subject: tuple[str, str],
        *,
        zone_id: str | None = None,
    ) -> list[NamespaceMount]:
        entries = await asyncio.to_thread(
            self._inner.get_mount_table,
            subject,
            zone_id=zone_id,
        )
        return [_to_namespace_mount(e, subject, zone_id) for e in entries]

    async def invalidate(self, subject: tuple[str, str]) -> None:
        await asyncio.to_thread(self._inner.invalidate, subject)
