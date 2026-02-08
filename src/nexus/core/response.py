"""Standardized response wrapper for backend operations.

This module provides a consistent response format for all backend content operations,
inspired by MindsDB's handler pattern. It enables:
- Consistent error handling across all backends
- Execution time tracking for observability
- Type-safe response unwrapping
- Standard factory methods for common response types

Usage:
    # Success response
    return HandlerResponse.ok(data=content_hash, backend_name="local")

    # Error response
    return HandlerResponse.error("Failed to write", code=500)

    # From exception
    return HandlerResponse.from_exception(e, backend_name="gcs")

    # Unwrap (raises exception on error)
    content = response.unwrap()
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, ParamSpec, TypeVar

if TYPE_CHECKING:
    pass  # Exceptions imported at runtime to avoid circular imports

T = TypeVar("T")


class ResponseType(Enum):
    """Response type enum for backend operations."""

    OK = "ok"
    ERROR = "error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"


@dataclass
class HandlerResponse(Generic[T]):
    """Standardized response wrapper for backend operations.

    Provides consistent error handling and response format across all backends.
    Inspired by MindsDB's HandlerResponse pattern.

    Attributes:
        resp_type: Response type (OK, ERROR, NOT_FOUND, CONFLICT)
        data: Response data (type depends on operation)
        error_message: Human-readable error description if failed
        error_code: HTTP-style error code (400, 404, 500, etc.)
        is_expected_error: True for expected errors (not_found, validation)
        execution_time_ms: Time taken for the operation in milliseconds
        backend_name: Name of the backend that produced this response
        path: File/content path for context
        affected_rows: Number of affected items (for batch operations)
    """

    resp_type: ResponseType
    data: T | None = None
    error_message: str | None = None
    error_code: int | None = None
    is_expected_error: bool = False
    execution_time_ms: float = 0.0
    backend_name: str | None = None
    path: str | None = None
    affected_rows: int = 0

    @property
    def success(self) -> bool:
        """Check if the response indicates success."""
        return self.resp_type == ResponseType.OK

    @classmethod
    def ok(
        cls,
        data: T,
        execution_time_ms: float = 0.0,
        backend_name: str | None = None,
        path: str | None = None,
        affected_rows: int = 0,
    ) -> HandlerResponse[T]:
        """Create a success response.

        Args:
            data: The response data
            execution_time_ms: Operation execution time in milliseconds
            backend_name: Name of the backend
            path: File/content path
            affected_rows: Number of affected items

        Returns:
            HandlerResponse with OK status
        """
        return cls(
            resp_type=ResponseType.OK,
            data=data,
            execution_time_ms=execution_time_ms,
            backend_name=backend_name,
            path=path,
            affected_rows=affected_rows,
        )

    @classmethod
    def error(
        cls,
        message: str,
        code: int = 500,
        is_expected: bool = False,
        execution_time_ms: float = 0.0,
        backend_name: str | None = None,
        path: str | None = None,
    ) -> HandlerResponse[Any]:
        """Create an error response.

        Args:
            message: Human-readable error description
            code: HTTP-style error code (default 500)
            is_expected: Whether this is an expected error
            execution_time_ms: Operation execution time in milliseconds
            backend_name: Name of the backend
            path: File/content path

        Returns:
            HandlerResponse with ERROR status
        """
        return HandlerResponse[Any](
            resp_type=ResponseType.ERROR,
            error_message=message,
            error_code=code,
            is_expected_error=is_expected,
            execution_time_ms=execution_time_ms,
            backend_name=backend_name,
            path=path,
        )

    @classmethod
    def not_found(
        cls,
        path: str,
        message: str | None = None,
        execution_time_ms: float = 0.0,
        backend_name: str | None = None,
    ) -> HandlerResponse[Any]:
        """Create a not-found response.

        Args:
            path: Path that was not found
            message: Optional custom message
            execution_time_ms: Operation execution time in milliseconds
            backend_name: Name of the backend

        Returns:
            HandlerResponse with NOT_FOUND status
        """
        return HandlerResponse[Any](
            resp_type=ResponseType.NOT_FOUND,
            error_message=message or f"Not found: {path}",
            error_code=404,
            is_expected_error=True,
            execution_time_ms=execution_time_ms,
            backend_name=backend_name,
            path=path,
        )

    @classmethod
    def conflict(
        cls,
        path: str,
        expected_etag: str,
        current_etag: str,
        execution_time_ms: float = 0.0,
        backend_name: str | None = None,
    ) -> HandlerResponse[Any]:
        """Create a conflict response for optimistic concurrency failures.

        Args:
            path: Path that had the conflict
            expected_etag: The expected etag value
            current_etag: The actual current etag value
            execution_time_ms: Operation execution time in milliseconds
            backend_name: Name of the backend

        Returns:
            HandlerResponse with CONFLICT status
        """
        message = (
            f"Conflict detected - file was modified. "
            f"Expected etag '{expected_etag[:16]}...', got '{current_etag[:16]}...'"
        )
        return HandlerResponse[Any](
            resp_type=ResponseType.CONFLICT,
            error_message=message,
            error_code=409,
            is_expected_error=True,
            execution_time_ms=execution_time_ms,
            backend_name=backend_name,
            path=path,
        )

    @classmethod
    def from_exception(
        cls,
        e: Exception,
        execution_time_ms: float = 0.0,
        backend_name: str | None = None,
        path: str | None = None,
    ) -> HandlerResponse[Any]:
        """Create a response from an exception.

        Maps common exception types to appropriate response types:
        - FileNotFoundError -> NOT_FOUND
        - NexusFileNotFoundError -> NOT_FOUND
        - ConflictError -> CONFLICT
        - Other -> ERROR

        Args:
            e: The exception to convert
            execution_time_ms: Operation execution time in milliseconds
            backend_name: Name of the backend
            path: File/content path (falls back to exception's path if available)

        Returns:
            HandlerResponse with appropriate status
        """
        # Import here to avoid circular imports
        from nexus.core.exceptions import ConflictError, NexusFileNotFoundError

        # Get path from exception if not provided
        exc_path = path or getattr(e, "path", None)

        # Check for expected error attribute
        is_expected = getattr(e, "is_expected", False)

        if isinstance(e, NexusFileNotFoundError):
            return cls.not_found(
                path=exc_path or "unknown",
                message=str(e),
                execution_time_ms=execution_time_ms,
                backend_name=backend_name,
            )
        elif isinstance(e, FileNotFoundError):
            return cls.not_found(
                path=exc_path or str(e),
                message=str(e),
                execution_time_ms=execution_time_ms,
                backend_name=backend_name,
            )
        elif isinstance(e, ConflictError):
            return cls.conflict(
                path=e.path or "unknown",
                expected_etag=e.expected_etag,
                current_etag=e.current_etag,
                execution_time_ms=execution_time_ms,
                backend_name=backend_name,
            )
        else:
            # Determine error code based on exception type
            code = 400 if is_expected else 500
            return cls.error(
                message=str(e),
                code=code,
                is_expected=is_expected,
                execution_time_ms=execution_time_ms,
                backend_name=backend_name,
                path=exc_path,
            )

    def unwrap(self) -> T:
        """Get data or raise appropriate exception.

        Returns the response data if successful, otherwise raises
        an appropriate exception based on the response type.

        Returns:
            The response data

        Raises:
            NexusFileNotFoundError: If resp_type is NOT_FOUND
            ConflictError: If resp_type is CONFLICT
            BackendError: For other error types
        """
        if self.success:
            return self.data  # type: ignore[return-value]

        # Import here to avoid circular imports
        from nexus.core.exceptions import BackendError, ConflictError, NexusFileNotFoundError

        if self.resp_type == ResponseType.NOT_FOUND:
            raise NexusFileNotFoundError(
                path=self.path or "unknown",
                message=self.error_message,
            )
        elif self.resp_type == ResponseType.CONFLICT:
            # For conflict, we need etag info - extract from message or use placeholders
            raise ConflictError(
                path=self.path or "unknown",
                expected_etag="unknown",
                current_etag="unknown",
            )
        else:
            raise BackendError(
                message=self.error_message or "Backend error",
                backend=self.backend_name,
                path=self.path,
            )

    def unwrap_or(self, default: T) -> T:
        """Get data or return default value.

        Args:
            default: Value to return if response is not successful

        Returns:
            The response data if successful, otherwise the default value
        """
        if self.success:
            return self.data  # type: ignore[return-value]
        return default

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization.

        Only includes non-None fields (except execution_time_ms which is always included).

        Returns:
            Dictionary representation of the response
        """
        result: dict[str, Any] = {
            "success": self.success,
            "resp_type": self.resp_type.value,
        }
        if self.data is not None:
            result["data"] = self.data
        if self.error_message:
            result["error_message"] = self.error_message
        if self.error_code is not None:
            result["error_code"] = self.error_code
        if self.is_expected_error:
            result["is_expected_error"] = self.is_expected_error
        if self.execution_time_ms > 0:
            result["execution_time_ms"] = self.execution_time_ms
        if self.backend_name:
            result["backend_name"] = self.backend_name
        if self.path:
            result["path"] = self.path
        if self.affected_rows > 0:
            result["affected_rows"] = self.affected_rows
        return result


P = ParamSpec("P")
R = TypeVar("R")


def timed_response(func: Callable[P, HandlerResponse[R]]) -> Callable[P, HandlerResponse[R]]:
    """Decorator to automatically track execution time for backend methods.

    Usage:
        @timed_response
        def read_content(self, hash: str) -> HandlerResponse[bytes]:
            # Method implementation...
            return HandlerResponse.ok(data=content, backend_name=self.name)

    The decorator will automatically set execution_time_ms on the returned response.
    """

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> HandlerResponse[R]:
        start = time.perf_counter()
        try:
            response = func(*args, **kwargs)
            if isinstance(response, HandlerResponse) and response.execution_time_ms == 0:
                response.execution_time_ms = (time.perf_counter() - start) * 1000
            return response
        except Exception as e:
            execution_time_ms = (time.perf_counter() - start) * 1000
            # Try to get backend_name from self (first arg)
            backend_name = getattr(args[0], "name", None) if args else None
            return HandlerResponse.from_exception(
                e, execution_time_ms=execution_time_ms, backend_name=backend_name
            )

    return wrapper
