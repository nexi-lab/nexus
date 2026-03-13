"""Nexus URN — path-based entity locators (Issue #2929).

A NexusURN addresses an entity within the system. URNs are *locators*, not
stable identities: they change on rename/move. Rename-stable resource IDs
are future work.

Format: ``urn:nexus:{entity_type}:{zone_id}:{identifier}``

The identifier is a deterministic hash of the entity's path, so any caller
can compute a URN from a path without a database lookup.

Examples:
    - ``urn:nexus:file:zone_acme:a1b2c3d4...``  (SHA-256 prefix of path)
    - ``urn:nexus:schema:zone_acme:abc123``
    - ``urn:nexus:user:zone_acme:alice``

Design decisions (Issue #2929):
    - URN is a locator, not stable identity (Key Decision #3)
    - Identifier derived from path via SHA-256 hash prefix
    - Rename → DELETE old URN + UPSERT new URN
    - ``NexusURN.from_metadata(meta)`` computes URN from FileMetadata
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

# Valid entity types for URN construction
VALID_ENTITY_TYPES = frozenset(
    {
        "file",
        "directory",
        "schema",
        "user",
        "tag",
        "aspect",
    }
)

_URN_PATTERN = re.compile(r"^urn:nexus:([a-z_]+):([a-zA-Z0-9_-]+):([a-zA-Z0-9_-]+)$")


@dataclass(frozen=True, slots=True)
class NexusURN:
    """Path-based entity locator.

    URNs are immutable value types. They change on rename/move because
    identity is derived from path, not a stable UUID.

    Attributes:
        entity_type: Type of entity (file, directory, schema, user, tag).
        zone_id: Zone/organization scope.
        identifier: Deterministic hash of the entity's path.
    """

    entity_type: str
    zone_id: str
    identifier: str

    def __post_init__(self) -> None:
        if not self.entity_type:
            raise ValueError("entity_type is required")
        if not self.zone_id:
            raise ValueError("zone_id is required")
        if not self.identifier:
            raise ValueError("identifier is required")

    def __str__(self) -> str:
        return f"urn:nexus:{self.entity_type}:{self.zone_id}:{self.identifier}"

    @classmethod
    def parse(cls, urn_string: str) -> NexusURN:
        """Parse a URN string into a NexusURN.

        Args:
            urn_string: URN in format ``urn:nexus:{type}:{zone}:{id}``.

        Returns:
            Parsed NexusURN instance.

        Raises:
            ValueError: If the string is not a valid Nexus URN.
        """
        match = _URN_PATTERN.match(urn_string)
        if not match:
            raise ValueError(
                f"Invalid Nexus URN format: {urn_string!r}. "
                f"Expected: urn:nexus:{{entity_type}}:{{zone_id}}:{{identifier}}"
            )
        return cls(
            entity_type=match.group(1),
            zone_id=match.group(2),
            identifier=match.group(3),
        )

    @staticmethod
    def _hash_path(path: str) -> str:
        """Compute deterministic identifier from a path."""
        return hashlib.sha256(path.encode()).hexdigest()[:32]

    @classmethod
    def for_file(cls, zone_id: str, path: str) -> NexusURN:
        """Create a URN for a file entity from its virtual path.

        The identifier is a deterministic SHA-256 hash prefix of the path,
        so any caller can compute the same URN without a database lookup.

        Args:
            zone_id: Zone the file belongs to.
            path: Virtual path of the file.

        Returns:
            File entity URN.
        """
        return cls(entity_type="file", zone_id=zone_id, identifier=cls._hash_path(path))

    @classmethod
    def for_directory(cls, zone_id: str, path: str) -> NexusURN:
        """Create a URN for a directory entity from its virtual path."""
        return cls(entity_type="directory", zone_id=zone_id, identifier=cls._hash_path(path))

    @classmethod
    def from_metadata(cls, meta: Any) -> NexusURN:
        """Compute a URN from FileMetadata.

        This is the primary entry point for computing a file URN from
        kernel-native FileMetadata. The URN is derived from the path.

        Args:
            meta: FileMetadata instance (or any object with ``path`` attribute).

        Returns:
            File entity URN using the metadata's path.
        """
        path: str = getattr(meta, "path", "")
        zone_id: str = getattr(meta, "zone_id", "default")
        return cls.for_file(zone_id=zone_id, path=path)

    def is_file(self) -> bool:
        return self.entity_type == "file"

    def is_directory(self) -> bool:
        return self.entity_type == "directory"
