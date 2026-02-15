"""Tuple CRUD and storage layer for ReBAC permissions.

Contains the data access layer for relationship tuples:
- TupleRepository: Connection management and tuple queries

Related: Issue #1459 (decomposition)
"""

from nexus.services.permissions.tuples.repository import TupleRepository

__all__ = [
    "TupleRepository",
]
