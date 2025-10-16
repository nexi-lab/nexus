"""Core components for Nexus filesystem."""

from nexus.core.embedded import Embedded
from nexus.core.exceptions import (
    BackendError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
)
from nexus.core.filesystem import NexusFilesystem

__all__ = [
    "NexusFilesystem",
    "Embedded",
    "NexusError",
    "NexusFileNotFoundError",
    "NexusPermissionError",
    "BackendError",
    "InvalidPathError",
    "MetadataError",
]
