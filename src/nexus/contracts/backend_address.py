"""Typed backend identifier: type + optional origin node address.

Tier-neutral value object — safe to import from any layer (kernel, services,
backends, tests). Zero runtime ``nexus.*`` imports.

The composite format ``"local@10.0.0.5:50051"`` stores both the backend type
and the node that owns the CAS content. This enables targeted content fetch
in federation (no broadcast) while remaining backward-compatible with legacy
entries that only store the type (e.g., ``"local"``).

Format:
    "{backend_type}"                    — type only (legacy / single-node)
    "{backend_type}@{host}:{port}"      — type + origin node address

Convention: Same ``id@host:port`` format used by ZoneManager peers.

Issue #1293 / #163: Federation content read path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackendAddress:
    """Parsed backend identifier carrying type and optional origin node.

    Examples:
        >>> BackendAddress.parse("local")
        BackendAddress(backend_type='local', origin=None)

        >>> BackendAddress.parse("local@10.0.0.5:50051")
        BackendAddress(backend_type='local', origin='10.0.0.5:50051')

        >>> str(BackendAddress("s3", "us-east-1.s3.example.com:443"))
        's3@us-east-1.s3.example.com:443'

        >>> BackendAddress.build("local", "10.0.0.5:50051")
        BackendAddress(backend_type='local', origin='10.0.0.5:50051')
    """

    backend_type: str
    origin: str | None = None

    @classmethod
    def parse(cls, raw: str) -> BackendAddress:
        """Parse a composite backend_name string.

        Args:
            raw: Backend name string, e.g. ``"local"`` or ``"local@10.0.0.5:50051"``.

        Returns:
            BackendAddress with parsed type and optional origin.

        Raises:
            ValueError: If raw is empty.
        """
        if not raw:
            raise ValueError("backend_name cannot be empty")
        if "@" in raw:
            backend_type, origin = raw.split("@", 1)
            return cls(backend_type=backend_type, origin=origin)
        return cls(backend_type=raw)

    @classmethod
    def build(cls, backend_type: str, origin: str | None = None) -> BackendAddress:
        """Construct a BackendAddress from components.

        Args:
            backend_type: Backend type name (e.g., "local", "s3", "gcs").
            origin: Optional node address (e.g., "10.0.0.5:50051").

        Returns:
            BackendAddress instance.
        """
        return cls(backend_type=backend_type, origin=origin)

    @property
    def has_origin(self) -> bool:
        """Whether this address includes an origin node."""
        return self.origin is not None

    def __str__(self) -> str:
        """Serialize to composite string for storage in FileMetadata.backend_name."""
        if self.origin:
            return f"{self.backend_type}@{self.origin}"
        return self.backend_type
