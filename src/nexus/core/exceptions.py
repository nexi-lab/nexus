"""Exception definitions for Nexus."""


class NexusError(Exception):
    """Base exception for all Nexus errors."""

    pass


class FileNotFoundError(NexusError):
    """Raised when a file is not found."""

    pass


class PermissionError(NexusError):
    """Raised when permission is denied."""

    pass


class BackendError(NexusError):
    """Raised when a backend operation fails."""

    pass


class AuthenticationError(NexusError):
    """Raised when authentication fails."""

    pass


class ValidationError(NexusError):
    """Raised when validation fails."""

    pass


class ConfigurationError(NexusError):
    """Raised when configuration is invalid."""

    pass


class LockError(NexusError):
    """Raised when lock acquisition fails."""

    pass


class CacheError(NexusError):
    """Raised when cache operation fails."""

    pass


class ParserError(NexusError):
    """Raised when parsing fails."""

    pass


class VectorDBError(NexusError):
    """Raised when vector database operation fails."""

    pass
