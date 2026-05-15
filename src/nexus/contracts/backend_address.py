"""Typed backend identifier: type + optional origin node address(es).

Tier-neutral value object — safe to import from any layer (kernel, services,
backends, tests). Zero runtime ``nexus.*`` imports.

The composite format ``"cas_local@10.0.0.5:50051"`` stores both the backend type
and the node that owns the CAS content. This enables targeted content fetch
in federation (no broadcast) while remaining backward-compatible with legacy
entries that only store the type (e.g., ``"cas_local"``).

Multi-origin format for content replication:
    ``"local@10.0.0.1:50051,10.0.0.2:50051"`` — content available on two nodes.

Format:
    "{backend_type}"                                — type only (legacy / single-node)
    "{backend_type}@{host}:{port}"                  — type + single origin
    "{backend_type}@{host1}:{port1},{host2}:{port2}"— type + multiple origins

Convention: Same ``id@host:port`` format used by ZoneManager peers.

Issue #1293 / #163: Federation content read path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackendAddress:
    """Parsed backend identifier carrying type and optional origin node(s).

    Supports both single-origin (``"local@10.0.0.1:50051"``) and multi-origin
    (``"local@10.0.0.1:50051,10.0.0.2:50051"``) formats for content replication.

    Examples:
        >>> BackendAddress.parse("cas_local")
        BackendAddress(backend_type='cas_local', origins=())

        >>> BackendAddress.parse("local@10.0.0.5:50051")
        BackendAddress(backend_type='local', origins=('10.0.0.5:50051',))

        >>> BackendAddress.parse("local@10.0.0.1:50051,10.0.0.2:50051")
        BackendAddress(backend_type='local', origins=('10.0.0.1:50051', '10.0.0.2:50051'))

        >>> str(BackendAddress("s3", ("us-east-1.s3.example.com:443",)))
        's3@us-east-1.s3.example.com:443'
    """

    backend_type: str
    origins: tuple[str, ...] = ()

    @property
    def has_origin(self) -> bool:
        """Whether this address includes at least one origin node."""
        return len(self.origins) > 0

    @classmethod
    def parse(cls, raw: str) -> BackendAddress:
        """Parse a composite backend_name string.

        Args:
            raw: Backend name string, e.g. ``"cas_local"``,
                 ``"local@10.0.0.5:50051"``, or
                 ``"local@10.0.0.1:50051,10.0.0.2:50051"``.

        Returns:
            BackendAddress with parsed type and origin(s).

        Raises:
            ValueError: If raw is empty.
        """
        if not raw:
            raise ValueError("backend_name cannot be empty")
        if "@" in raw:
            backend_type, origins_str = raw.split("@", 1)
            origins = tuple(o.strip() for o in origins_str.split(","))
            return cls(backend_type=backend_type, origins=origins)
        return cls(backend_type=raw)

    @classmethod
    def build(cls, backend_type: str, origin: str | None = None) -> BackendAddress:
        """Construct a BackendAddress from components (single origin).

        Args:
            backend_type: Backend type name (e.g., "cas_local", "s3", "gcs").
            origin: Optional node address (e.g., "10.0.0.5:50051").

        Returns:
            BackendAddress instance.
        """
        origins = (origin,) if origin is not None else ()
        return cls(backend_type=backend_type, origins=origins)

    def with_origin(self, addr: str) -> BackendAddress:
        """Return a new BackendAddress with an additional origin appended.

        If ``addr`` is already present, returns self (idempotent).
        """
        if addr in self.origins:
            return self
        return BackendAddress(self.backend_type, (*self.origins, addr))

    def __str__(self) -> str:
        """Serialize to a composite string used by routing/wiring layers."""
        if self.origins:
            return f"{self.backend_type}@{','.join(self.origins)}"
        return self.backend_type
