"""Shared utilities for the Memory brick."""

from collections.abc import Callable
from typing import Any


def batch_operation(
    ids: list[str],
    operation: Callable[[str], bool],
    success_key: str = "success",
) -> dict[str, Any]:
    """Execute a batch operation with success/failure tracking.

    Args:
        ids: List of IDs to operate on.
        operation: Callable taking an ID and returning bool.
        success_key: Name for the success count in the result dict.

    Returns:
        Dict with success/failure counts and ID lists.
    """
    success_ids: list[str] = []
    failed_ids: list[str] = []

    for item_id in ids:
        if operation(item_id):
            success_ids.append(item_id)
        else:
            failed_ids.append(item_id)

    return {
        success_key: len(success_ids),
        "failed": len(failed_ids),
        f"{success_key}_ids": success_ids,
        "failed_ids": failed_ids,
    }
