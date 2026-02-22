"""OperationResult — re-exports from contracts layer.

Canonical location: ``nexus.contracts.operation_result``.
This module re-exports for backward compatibility with existing imports.
"""

from nexus.contracts.operation_result import OperationResult, OperationWarning

__all__ = ["OperationResult", "OperationWarning"]
