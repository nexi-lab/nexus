"""WirableFS factory wiring contract (Issue #2133).

Defines the contract for what ``_boot_wired_services()`` needs from NexusFS.
This replaces the ``Any`` type on the ``nx`` parameter, giving the wiring
layer typed attribute access instead of ``getattr()`` calls.

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - Issue #2133: Break circular runtime imports between services/ and core/
    - Issue #2359: Moved from core/protocols/ to contracts/ (cross-tier)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.contracts.types import OperationContext
    from nexus.core.metastore import MetastoreABC
    from nexus.services.protocols.permission_enforcer import PermissionEnforcerProtocol
    from nexus.storage.record_store import RecordStoreABC


@runtime_checkable
class WirableFS(Protocol):
    """Contract for NexusFS attributes accessed during wiring.

    ``_boot_wired_services()`` uses these attributes to construct
    Tier 2b services. This protocol replaces ``Any`` and eliminates
    all ``getattr()`` calls in the wiring layer.

    Do NOT use ``isinstance()`` checks in hot paths.
    """

    @property
    def metadata(self) -> MetastoreABC: ...

    @property
    def backend(self) -> Backend: ...

    def read(self, path: str, **kwargs: Any) -> bytes: ...

    _enforce_permissions: bool
    _permission_enforcer: PermissionEnforcerProtocol | None
    _record_store: RecordStoreABC | None
    _default_context: OperationContext | None
    _config: Any
