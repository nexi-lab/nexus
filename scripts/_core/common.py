"""
Common utilities for provisioning operations.

Provides shared helper functions for error handling, operation execution,
and consistent output formatting.
"""

from collections.abc import Callable
from typing import Any


def safe_operation(
    operation_name: str,
    operation_fn: Callable[..., Any],
    *args: Any,
    on_success: Callable[[Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    **kwargs: Any,
) -> Any | None:
    """
    Execute an operation with consistent error handling and output.

    This wrapper reduces boilerplate try-except blocks throughout the codebase
    by providing standardized success/failure messages and optional callbacks.

    Args:
        operation_name: Human-readable operation description
        operation_fn: Function to execute
        *args: Positional arguments to pass to operation_fn
        on_success: Optional callback to run on success (receives result)
        on_error: Optional callback to run on error (receives exception)
        **kwargs: Keyword arguments to pass to operation_fn

    Returns:
        Result of operation_fn on success, None on failure

    Examples:
        >>> def create_folder(path):
        ...     nx.mkdir(path)
        ...     return {"path": path}
        >>>
        >>> result = safe_operation(
        ...     "Creating user folder",
        ...     create_folder,
        ...     "/tenant:default/user:alice"
        ... )
        ✓ Creating user folder

        >>> # With error handling
        >>> safe_operation(
        ...     "Deleting resource",
        ...     nx.delete,
        ...     "/nonexistent",
        ...     on_error=lambda e: metrics.increment("delete_errors")
        ... )
        ✗ Deleting resource: FileNotFoundError: /nonexistent
    """
    try:
        result = operation_fn(*args, **kwargs)
        print(f"  ✓ {operation_name}")

        if on_success:
            on_success(result)

        return result

    except Exception as e:
        print(f"  ✗ {operation_name}: {e}")

        if on_error:
            on_error(e)

        return None


def print_section(title: str, char: str = "=") -> None:
    """
    Print a formatted section header.

    Args:
        title: Section title text
        char: Character to use for separator line

    Examples:
        >>> print_section("System Provisioning")
        ========================================
        System Provisioning
        ========================================
    """
    separator = char * len(title)
    print(f"\n{separator}")
    print(title)
    print(separator)


def print_subsection(title: str) -> None:
    """
    Print a formatted subsection header.

    Args:
        title: Subsection title text

    Examples:
        >>> print_subsection("Creating directories")

        --- Creating directories ---
    """
    print(f"\n--- {title} ---")
