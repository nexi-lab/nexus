"""Declarative VFS hook specification for service lifecycle.

Services declare which VFS hooks they need at mount time via ``HookSpec``.
The ``ServiceLifecycleCoordinator`` registers/unregisters these hooks
during mount/unmount/swap, enabling clean hook teardown on hot-swap.

Issue #1452 Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HookSpec:
    """Declares VFS hooks a service needs at mount time.

    Each field is a tuple of hook objects to register with KernelDispatch.
    On mount, all hooks are registered; on unmount, all are unregistered.

    Example::

        spec = HookSpec(
            read_hooks=(my_read_hook,),
            observers=(my_observer,),
        )
    """

    read_hooks: tuple[Any, ...] = ()
    write_hooks: tuple[Any, ...] = ()
    write_batch_hooks: tuple[Any, ...] = ()
    delete_hooks: tuple[Any, ...] = ()
    rename_hooks: tuple[Any, ...] = ()
    mkdir_hooks: tuple[Any, ...] = ()
    rmdir_hooks: tuple[Any, ...] = ()
    observers: tuple[Any, ...] = ()
    resolvers: tuple[Any, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True if no hooks are declared."""
        return not any(
            (
                self.read_hooks,
                self.write_hooks,
                self.write_batch_hooks,
                self.delete_hooks,
                self.rename_hooks,
                self.mkdir_hooks,
                self.rmdir_hooks,
                self.observers,
                self.resolvers,
            )
        )

    @property
    def total_hooks(self) -> int:
        """Total number of hook objects across all categories."""
        return (
            len(self.read_hooks)
            + len(self.write_hooks)
            + len(self.write_batch_hooks)
            + len(self.delete_hooks)
            + len(self.rename_hooks)
            + len(self.mkdir_hooks)
            + len(self.rmdir_hooks)
            + len(self.observers)
            + len(self.resolvers)
        )
