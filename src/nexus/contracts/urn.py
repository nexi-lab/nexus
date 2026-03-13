"""Nexus URN — stable, location-independent entity identifiers (Issue #2929).

A NexusURN uniquely identifies an entity across the system. URNs are stable:
they do NOT change on rename/move. The path is stored as an aspect, not
embedded in the URN.

Format: ``urn:nexus:{entity_type}:{zone_id}:{identifier}``

Examples:
    - ``urn:nexus:file:zone_acme:550e8400-e29b-41d4-a716-446655440000``
    - ``urn:nexus:schema:zone_acme:abc123``
    - ``urn:nexus:user:zone_acme:alice``

Design decisions (Issue #2929, Architecture Review #1):
    - UUID-based stable identity (not path-based locator)
    - ``FilePathModel.path_id`` serves as the file entity identifier
    - Path is an aspect, not part of the URN
    - URN construction belongs in the service layer, not on this dataclass
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
    """Stable, location-independent entity identifier.

    URNs are immutable value types. They survive renames and moves
    because identity is based on UUID, not path.

    Attributes:
        entity_type: Type of entity (file, directory, schema, user, tag).
        zone_id: Zone/organization scope.
        identifier: Unique identifier within the entity type and zone.
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

    @classmethod
    def for_file(cls, zone_id: str, path_id: str) -> NexusURN:
        """Create a URN for a file entity using its path_id.

        This is the primary factory for file URNs. The path_id comes
        from ``FilePathModel.path_id`` (UUID).

        Args:
            zone_id: Zone the file belongs to.
            path_id: UUID primary key from FilePathModel.

        Returns:
            File entity URN.
        """
        return cls(entity_type="file", zone_id=zone_id, identifier=path_id)

    @classmethod
    def for_directory(cls, zone_id: str, path_id: str) -> NexusURN:
        """Create a URN for a directory entity."""
        return cls(entity_type="directory", zone_id=zone_id, identifier=path_id)

    def is_file(self) -> bool:
        return self.entity_type == "file"

    def is_directory(self) -> bool:
        return self.entity_type == "directory"
