"""Shared append-only query infrastructure.

Issue #1360: Extracted from OperationLogger so that ExchangeAuditLogger
can reuse cursor-based pagination, filter application, and counting.

Both loggers compose this mixin rather than inheriting from it,
keeping each store's public API explicit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, desc, func, select
from sqlalchemy.orm import Session


class AppendOnlyQueryMixin:
    """Reusable query helpers for append-only log tables.

    Callers must supply:
    - model_class: the SQLAlchemy model to query
    - id_column_name: name of the primary-key column (for cursor tie-breaking)
    - created_column_name: name of the timestamp column (for ordering)
    """

    def __init__(
        self,
        *,
        model_class: type,
        id_column_name: str = "id",
        created_column_name: str = "created_at",
    ) -> None:
        self._model = model_class
        self._id_col_name = id_column_name
        self._created_col_name = created_column_name

    @property
    def _id_col(self) -> Any:
        return getattr(self._model, self._id_col_name)

    @property
    def _created_col(self) -> Any:
        return getattr(self._model, self._created_col_name)

    # ------------------------------------------------------------------
    # Generic filter application
    # ------------------------------------------------------------------

    def apply_filters(
        self,
        stmt: Any,
        *,
        filters: dict[str, Any],
    ) -> Any:
        """Apply a dict of {column_name: value} filters to a statement.

        Special keys:
        - ``since`` → created_at >= value
        - ``until`` → created_at <= value
        - ``amount_min`` → amount >= value
        - ``amount_max`` → amount <= value

        All other keys are treated as exact-match filters on the model column.
        None values are silently skipped.
        """
        for key, value in filters.items():
            if value is None:
                continue
            if key == "since":
                stmt = stmt.where(self._created_col >= value)
            elif key == "until":
                stmt = stmt.where(self._created_col <= value)
            elif key == "amount_min":
                amount_col = getattr(self._model, "amount", None)
                if amount_col is not None:
                    stmt = stmt.where(amount_col >= value)
            elif key == "amount_max":
                amount_col = getattr(self._model, "amount", None)
                if amount_col is not None:
                    stmt = stmt.where(amount_col <= value)
            else:
                col = getattr(self._model, key, None)
                if col is not None:
                    stmt = stmt.where(col == value)
        return stmt

    # ------------------------------------------------------------------
    # Cursor-based pagination
    # ------------------------------------------------------------------

    def list_cursor(
        self,
        session: Session,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Any], str | None]:
        """Fetch rows with cursor-based pagination (newest first).

        Args:
            session: Active SQLAlchemy session.
            filters: Column filters (see ``apply_filters``).
            limit: Page size.
            cursor: ID of the last item from the previous page.

        Returns:
            (rows, next_cursor) — next_cursor is None when no more rows exist.
        """
        stmt: Select[Any] = select(self._model).order_by(
            desc(self._created_col),
            desc(self._id_col),
        )

        if cursor is not None:
            cursor_row = session.execute(
                select(self._model).where(self._id_col == cursor)
            ).scalar_one_or_none()
            if cursor_row is not None:
                cursor_ts = getattr(cursor_row, self._created_col_name)
                stmt = stmt.where(
                    (self._created_col < cursor_ts)
                    | ((self._created_col == cursor_ts) & (self._id_col < cursor))
                )

        if filters:
            stmt = self.apply_filters(stmt, filters=filters)

        stmt = stmt.limit(limit + 1)
        results = list(session.execute(stmt).scalars())

        if len(results) > limit:
            results = results[:limit]
            next_cursor: str | None = getattr(results[-1], self._id_col_name)
        else:
            next_cursor = None

        return results, next_cursor

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    def count(
        self,
        session: Session,
        *,
        filters: dict[str, Any] | None = None,
    ) -> int:
        """Count rows matching the given filters."""
        stmt = select(func.count()).select_from(self._model)
        if filters:
            stmt = self.apply_filters(stmt, filters=filters)
        result: int = session.execute(stmt).scalar_one()
        return result
