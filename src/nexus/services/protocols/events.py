"""Events protocols — re-exports from split modules.

The former ``EventsProtocol`` bundled file watching (S8) and advisory
locking (S9) into a single Protocol.  Per ops-scenario-matrix.md §2.2.2
these are fundamentally different subsystems (inotify vs flock) and are
now defined separately:

- :class:`WatchProtocol` — file change long-poll (``watch.py``)
- :class:`LockProtocol`  — advisory lock lifecycle (``lock.py``)

References:
    - docs/architecture/ops-scenario-matrix.md §2.2.2
    - Issue #1287: Extract NexusFS domain services from god object
"""

from nexus.services.protocols.lock import LockProtocol
from nexus.services.protocols.watch import WatchProtocol

__all__ = ["LockProtocol", "WatchProtocol"]
