"""Custom exceptions for Nexus filesystem operations."""


class NexusError(Exception):
    """Base exception for all Nexus errors."""

    def __init__(self, message: str, path: str | None = None):
        self.message = message
        self.path = path
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format error message with optional path."""
        if self.path:
            return f"{self.message}: {self.path}"
        return self.message


class NexusFileNotFoundError(NexusError):
    """Raised when a file or directory does not exist."""

    def __init__(self, path: str, message: str | None = None):
        msg = message or "File not found"
        super().__init__(msg, path)


class NexusPermissionError(NexusError):
    """Raised when access to a file or directory is denied."""

    def __init__(self, path: str, message: str | None = None):
        msg = message or "Permission denied"
        super().__init__(msg, path)


class BackendError(NexusError):
    """Raised when a backend operation fails."""

    def __init__(self, message: str, backend: str | None = None, path: str | None = None):
        self.backend = backend
        if backend:
            message = f"[{backend}] {message}"
        super().__init__(message, path)


class InvalidPathError(NexusError):
    """Raised when a path is invalid or contains illegal characters."""

    def __init__(self, path: str, message: str | None = None):
        msg = message or "Invalid path"
        super().__init__(msg, path)


class MetadataError(NexusError):
    """Raised when metadata operations fail."""

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message, path)
