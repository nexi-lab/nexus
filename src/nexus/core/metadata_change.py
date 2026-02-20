"""MetadataChange — revision-ordered change event from the Metastore.

Used by ``WatchCacheManager`` to poll metadata changes from the underlying
store and route them through ``ReadSetAwareCache.invalidate_for_write()``
for proactive cache invalidation in multi-node deployments.

Pattern follows ``MutationEvent`` in ``lib/mutation_hooks.py``.

Issue #2065.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetadataChange:
    """A single metadata change event from the Metastore.

    Attributes:
        revision: Zone revision of the change.
        path: Affected virtual path.
        operation: "put" or "delete" (str for simpler FFI, not enum).
        zone_id: Zone where the change occurred.
    """

    revision: int
    path: str
    operation: str
    zone_id: str
