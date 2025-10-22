"""Query builder for work detection queries in Nexus metadata store.

Provides efficient SQL view-based queries for work item processing:
- Ready work (no blocking dependencies)
- Pending work (ordered by priority)
- Blocked work (has incomplete dependencies)
- In-progress work (currently being processed)
- Work ordered by priority
"""

from __future__ import annotations

import builtins
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from nexus.core.exceptions import MetadataError


class WorkQueryBuilder:
    """
    Query builder for work detection using SQL views.

    Provides methods to query work items in various states using
    pre-defined SQL views for optimal performance.
    """

    @staticmethod
    def get_ready_work(session: Session, limit: int | None = None) -> builtins.list[dict[str, Any]]:
        """Get files that are ready for processing.

        Uses the ready_work_items SQL view which efficiently finds files with:
        - status='ready'
        - No blocking dependencies

        Args:
            session: SQLAlchemy session
            limit: Optional limit on number of results

        Returns:
            List of work item dicts with path, status, priority, etc.

        Raises:
            MetadataError: If query fails
        """
        try:
            query = "SELECT * FROM ready_work_items"
            if limit:
                query += f" LIMIT {limit}"

            result = session.execute(text(query))
            rows = result.fetchall()

            return [
                {
                    "path_id": row[0],
                    "tenant_id": row[1],
                    "virtual_path": row[2],
                    "backend_id": row[3],
                    "physical_path": row[4],
                    "file_type": row[5],
                    "size_bytes": row[6],
                    "content_hash": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                    "status": json.loads(row[10]) if row[10] else None,
                    "priority": json.loads(row[11]) if row[11] else None,
                }
                for row in rows
            ]
        except Exception as e:
            raise MetadataError(f"Failed to get ready work: {e}") from e

    @staticmethod
    def get_pending_work(
        session: Session, limit: int | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Get files with status='pending' ordered by priority.

        Uses the pending_work_items SQL view.

        Args:
            session: SQLAlchemy session
            limit: Optional limit on number of results

        Returns:
            List of work item dicts

        Raises:
            MetadataError: If query fails
        """
        try:
            query = "SELECT * FROM pending_work_items"
            if limit:
                query += f" LIMIT {limit}"

            result = session.execute(text(query))
            rows = result.fetchall()

            return [
                {
                    "path_id": row[0],
                    "tenant_id": row[1],
                    "virtual_path": row[2],
                    "backend_id": row[3],
                    "physical_path": row[4],
                    "file_type": row[5],
                    "size_bytes": row[6],
                    "content_hash": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                    "status": json.loads(row[10]) if row[10] else None,
                    "priority": json.loads(row[11]) if row[11] else None,
                }
                for row in rows
            ]
        except Exception as e:
            raise MetadataError(f"Failed to get pending work: {e}") from e

    @staticmethod
    def get_blocked_work(
        session: Session, limit: int | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Get files that are blocked by dependencies.

        Uses the blocked_work_items SQL view.

        Args:
            session: SQLAlchemy session
            limit: Optional limit on number of results

        Returns:
            List of work item dicts with blocker_count

        Raises:
            MetadataError: If query fails
        """
        try:
            query = "SELECT * FROM blocked_work_items"
            if limit:
                query += f" LIMIT {limit}"

            result = session.execute(text(query))
            rows = result.fetchall()

            return [
                {
                    "path_id": row[0],
                    "tenant_id": row[1],
                    "virtual_path": row[2],
                    "backend_id": row[3],
                    "physical_path": row[4],
                    "file_type": row[5],
                    "size_bytes": row[6],
                    "content_hash": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                    "status": json.loads(row[10]) if row[10] else None,
                    "priority": json.loads(row[11]) if row[11] else None,
                    "blocker_count": row[12],
                }
                for row in rows
            ]
        except Exception as e:
            raise MetadataError(f"Failed to get blocked work: {e}") from e

    @staticmethod
    def get_in_progress_work(
        session: Session, limit: int | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Get files currently being processed.

        Uses the in_progress_work SQL view.

        Args:
            session: SQLAlchemy session
            limit: Optional limit on number of results

        Returns:
            List of work item dicts with worker_id and started_at

        Raises:
            MetadataError: If query fails
        """
        try:
            query = "SELECT * FROM in_progress_work"
            if limit:
                query += f" LIMIT {limit}"

            result = session.execute(text(query))
            rows = result.fetchall()

            return [
                {
                    "path_id": row[0],
                    "tenant_id": row[1],
                    "virtual_path": row[2],
                    "backend_id": row[3],
                    "file_type": row[4],
                    "size_bytes": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "status": json.loads(row[8]) if row[8] else None,
                    "worker_id": json.loads(row[9]) if row[9] else None,
                    "started_at": json.loads(row[10]) if row[10] else None,
                }
                for row in rows
            ]
        except Exception as e:
            raise MetadataError(f"Failed to get in-progress work: {e}") from e

    @staticmethod
    def get_work_by_priority(
        session: Session, limit: int | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Get all work items ordered by priority.

        Uses the work_by_priority SQL view.

        Args:
            session: SQLAlchemy session
            limit: Optional limit on number of results

        Returns:
            List of work item dicts

        Raises:
            MetadataError: If query fails
        """
        try:
            query = "SELECT * FROM work_by_priority"
            if limit:
                query += f" LIMIT {limit}"

            result = session.execute(text(query))
            rows = result.fetchall()

            return [
                {
                    "path_id": row[0],
                    "tenant_id": row[1],
                    "virtual_path": row[2],
                    "backend_id": row[3],
                    "file_type": row[4],
                    "size_bytes": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                    "status": json.loads(row[8]) if row[8] else None,
                    "priority": json.loads(row[9]) if row[9] else None,
                    "tags": json.loads(row[10]) if row[10] else None,
                }
                for row in rows
            ]
        except Exception as e:
            raise MetadataError(f"Failed to get work by priority: {e}") from e
