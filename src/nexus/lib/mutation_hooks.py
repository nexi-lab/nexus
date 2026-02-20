"""Post-mutation hook infrastructure for kernel file operations.

The kernel fires ``MutationEvent`` after every successful write/delete/rename.
Interested observers implement ``PostMutationHook`` and register via
``NexusFS.register_mutation_hook()``. The kernel iterates the hook list
once — it does NOT know which specific observers are registered.

This replaces the per-attribute observer pattern that required a new
kernel attribute + call site for every new observer. Now adding a
new post-mutation observer is:
    1. Implement ``PostMutationHook``
    2. Call ``nexus_fs.register_mutation_hook(my_hook)``

Error policy: fire-and-forget. Hook failures are logged but never abort
the mutation. For audit-critical observers that CAN abort operations,
use ``_write_observer.on_write()`` / ``on_delete()`` / ``on_rename()`` instead.
Observer and hook are independent kernel subsystems (cf. Linux LSM vs notifier chains).

Issue #625 partial.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class MutationOp(Enum):
    """Kernel mutation operation types."""

    WRITE = "write"
    DELETE = "delete"
    RENAME = "rename"
    MKDIR = "mkdir"
    RMDIR = "rmdir"


@dataclass(frozen=True, slots=True)
class MutationEvent:
    """Standardized kernel mutation event.

    Carries all context that post-mutation hooks might need.
    Each hook extracts what it requires and ignores the rest.
    """

    operation: MutationOp
    path: str
    zone_id: str
    revision: int

    # Common optional context
    agent_id: str | None = None
    user_id: str | None = None
    timestamp: str | None = None
    etag: str | None = None
    size: int | None = None

    # Write-specific
    version: int | None = None
    is_new: bool = False

    # Rename-specific
    new_path: str | None = None


@runtime_checkable
class PostMutationHook(Protocol):
    """Observer for kernel file mutations (fire-and-forget).

    Implement this protocol and register via ``register_mutation_hook()``
    to receive notifications after every write/delete/rename.
    """

    def on_mutation(self, event: MutationEvent) -> None:
        """Called after a successful kernel mutation.

        Must not raise — exceptions are caught and logged by the kernel.
        """
        ...
