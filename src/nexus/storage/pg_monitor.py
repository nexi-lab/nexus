"""PostgreSQL monitoring utilities based on Postgres Best Practices.

Provides query analysis and diagnostics using pg_stat_statements.
Reference: https://supabase.com/docs/guides/database/extensions/pg_stat_statements

Usage:
    from nexus.storage.pg_monitor import PgMonitor

    # Initialize with SQLAlchemy session
    monitor = PgMonitor(session)

    # Get slowest queries
    slow_queries = monitor.get_slowest_queries(limit=10)

    # Get most frequent queries
    frequent_queries = monitor.get_most_frequent_queries(limit=10)

    # Get missing FK indexes
    missing_fk_indexes = monitor.find_missing_fk_indexes()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class QueryStats:
    """Statistics for a single query from pg_stat_statements."""

    query: str
    calls: int
    total_time_ms: float
    mean_time_ms: float
    min_time_ms: float
    max_time_ms: float
    rows: int


@dataclass
class MissingFKIndex:
    """Information about a missing foreign key index."""

    table_name: str
    fk_column: str
    referenced_table: str
    referenced_column: str


@dataclass
class TableStats:
    """VACUUM/ANALYZE statistics for a table."""

    table_name: str
    last_vacuum: str | None
    last_autovacuum: str | None
    last_analyze: str | None
    last_autoanalyze: str | None
    dead_tuples: int
    live_tuples: int


class PgMonitor:
    """PostgreSQL monitoring utilities.

    Provides methods to analyze query performance, identify missing indexes,
    and check table statistics using pg_stat_statements.

    Based on Supabase Postgres Best Practices guide.
    """

    def __init__(self, session: Session):
        """Initialize the monitor with a SQLAlchemy session.

        Args:
            session: SQLAlchemy session connected to PostgreSQL
        """
        self.session = session

    def is_postgres(self) -> bool:
        """Check if the database is PostgreSQL."""
        try:
            result = self.session.execute(text("SELECT version()"))
            version = result.scalar()
            return version is not None and "PostgreSQL" in str(version)
        except Exception:
            return False

    def is_pg_stat_statements_enabled(self) -> bool:
        """Check if pg_stat_statements extension is enabled."""
        try:
            result = self.session.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")
            )
            return result.scalar() is not None
        except Exception:
            return False

    def get_slowest_queries(self, limit: int = 10) -> list[QueryStats]:
        """Get the slowest queries by total execution time.

        Reference: https://supabase.com/docs/guides/database/extensions/pg_stat_statements

        Args:
            limit: Maximum number of queries to return

        Returns:
            List of QueryStats sorted by total time descending
        """
        if not self.is_pg_stat_statements_enabled():
            logger.warning("pg_stat_statements not enabled")
            return []

        result = self.session.execute(
            text(
                """
                SELECT
                    query,
                    calls,
                    round(total_exec_time::numeric, 2) as total_time_ms,
                    round(mean_exec_time::numeric, 2) as mean_time_ms,
                    round(min_exec_time::numeric, 2) as min_time_ms,
                    round(max_exec_time::numeric, 2) as max_time_ms,
                    rows
                FROM pg_stat_statements
                ORDER BY total_exec_time DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )

        return [
            QueryStats(
                query=row.query,
                calls=row.calls,
                total_time_ms=float(row.total_time_ms),
                mean_time_ms=float(row.mean_time_ms),
                min_time_ms=float(row.min_time_ms),
                max_time_ms=float(row.max_time_ms),
                rows=row.rows,
            )
            for row in result
        ]

    def get_most_frequent_queries(self, limit: int = 10) -> list[QueryStats]:
        """Get the most frequently executed queries.

        Args:
            limit: Maximum number of queries to return

        Returns:
            List of QueryStats sorted by call count descending
        """
        if not self.is_pg_stat_statements_enabled():
            logger.warning("pg_stat_statements not enabled")
            return []

        result = self.session.execute(
            text(
                """
                SELECT
                    query,
                    calls,
                    round(total_exec_time::numeric, 2) as total_time_ms,
                    round(mean_exec_time::numeric, 2) as mean_time_ms,
                    round(min_exec_time::numeric, 2) as min_time_ms,
                    round(max_exec_time::numeric, 2) as max_time_ms,
                    rows
                FROM pg_stat_statements
                ORDER BY calls DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )

        return [
            QueryStats(
                query=row.query,
                calls=row.calls,
                total_time_ms=float(row.total_time_ms),
                mean_time_ms=float(row.mean_time_ms),
                min_time_ms=float(row.min_time_ms),
                max_time_ms=float(row.max_time_ms),
                rows=row.rows,
            )
            for row in result
        ]

    def get_slow_average_queries(
        self, min_mean_time_ms: float = 100, limit: int = 10
    ) -> list[QueryStats]:
        """Get queries with high average execution time.

        These are candidates for optimization (adding indexes, query rewrite).

        Args:
            min_mean_time_ms: Minimum mean execution time in milliseconds
            limit: Maximum number of queries to return

        Returns:
            List of QueryStats sorted by mean time descending
        """
        if not self.is_pg_stat_statements_enabled():
            logger.warning("pg_stat_statements not enabled")
            return []

        result = self.session.execute(
            text(
                """
                SELECT
                    query,
                    calls,
                    round(total_exec_time::numeric, 2) as total_time_ms,
                    round(mean_exec_time::numeric, 2) as mean_time_ms,
                    round(min_exec_time::numeric, 2) as min_time_ms,
                    round(max_exec_time::numeric, 2) as max_time_ms,
                    rows
                FROM pg_stat_statements
                WHERE mean_exec_time > :min_mean_time
                ORDER BY mean_exec_time DESC
                LIMIT :limit
                """
            ),
            {"min_mean_time": min_mean_time_ms, "limit": limit},
        )

        return [
            QueryStats(
                query=row.query,
                calls=row.calls,
                total_time_ms=float(row.total_time_ms),
                mean_time_ms=float(row.mean_time_ms),
                min_time_ms=float(row.min_time_ms),
                max_time_ms=float(row.max_time_ms),
                rows=row.rows,
            )
            for row in result
        ]

    def find_missing_fk_indexes(self) -> list[MissingFKIndex]:
        """Find foreign key columns that lack indexes.

        Missing FK indexes cause slow JOINs and CASCADE operations.
        Reference: https://www.postgresql.org/docs/current/ddl-constraints.html#DDL-CONSTRAINTS-FK

        Returns:
            List of MissingFKIndex describing columns that need indexes
        """
        result = self.session.execute(
            text(
                """
                SELECT
                    conrelid::regclass AS table_name,
                    a.attname AS fk_column,
                    confrelid::regclass AS referenced_table,
                    af.attname AS referenced_column
                FROM pg_constraint c
                JOIN pg_attribute a ON a.attrelid = c.conrelid
                    AND a.attnum = ANY(c.conkey)
                JOIN pg_attribute af ON af.attrelid = c.confrelid
                    AND af.attnum = ANY(c.confkey)
                WHERE c.contype = 'f'
                AND NOT EXISTS (
                    SELECT 1 FROM pg_index i
                    WHERE i.indrelid = c.conrelid
                    AND a.attnum = ANY(i.indkey)
                )
                ORDER BY table_name, fk_column
                """
            )
        )

        return [
            MissingFKIndex(
                table_name=str(row.table_name),
                fk_column=row.fk_column,
                referenced_table=str(row.referenced_table),
                referenced_column=row.referenced_column,
            )
            for row in result
        ]

    def get_table_stats(self, table_names: list[str] | None = None) -> list[TableStats]:
        """Get VACUUM and ANALYZE statistics for tables.

        Outdated statistics cause poor query plans.
        Reference: https://supabase.com/docs/guides/database/database-size#vacuum-operations

        Args:
            table_names: Optional list of specific tables to check.
                        If None, returns stats for all user tables.

        Returns:
            List of TableStats sorted by last_analyze (oldest first)
        """
        query = """
            SELECT
                relname AS table_name,
                last_vacuum::text,
                last_autovacuum::text,
                last_analyze::text,
                last_autoanalyze::text,
                n_dead_tup AS dead_tuples,
                n_live_tup AS live_tuples
            FROM pg_stat_user_tables
        """

        params: dict[str, Any] = {}
        if table_names:
            query += " WHERE relname = ANY(:table_names)"
            params["table_names"] = table_names

        query += " ORDER BY last_analyze NULLS FIRST"

        result = self.session.execute(text(query), params)

        return [
            TableStats(
                table_name=row.table_name,
                last_vacuum=row.last_vacuum,
                last_autovacuum=row.last_autovacuum,
                last_analyze=row.last_analyze,
                last_autoanalyze=row.last_autoanalyze,
                dead_tuples=row.dead_tuples or 0,
                live_tuples=row.live_tuples or 0,
            )
            for row in result
        ]

    def get_connection_stats(self) -> dict[str, Any]:
        """Get current connection statistics.

        Useful for monitoring connection pool usage.
        Reference: https://supabase.com/docs/guides/platform/performance#connection-management

        Returns:
            Dictionary with connection statistics
        """
        result = self.session.execute(
            text(
                """
                SELECT
                    count(*) AS total_connections,
                    count(*) FILTER (WHERE state = 'active') AS active,
                    count(*) FILTER (WHERE state = 'idle') AS idle,
                    count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_transaction,
                    count(*) FILTER (WHERE state = 'idle in transaction (aborted)') AS idle_in_transaction_aborted,
                    count(*) FILTER (WHERE wait_event_type IS NOT NULL) AS waiting
                FROM pg_stat_activity
                WHERE datname = current_database()
                """
            )
        )
        row = result.fetchone()
        if row:
            return {
                "total_connections": row.total_connections,
                "active": row.active,
                "idle": row.idle,
                "idle_in_transaction": row.idle_in_transaction,
                "idle_in_transaction_aborted": row.idle_in_transaction_aborted,
                "waiting": row.waiting,
            }
        return {}

    def get_index_usage(self, min_size_mb: float = 1.0) -> list[dict[str, Any]]:
        """Get index usage statistics.

        Identifies unused indexes that could be dropped to save space.

        Args:
            min_size_mb: Minimum index size in MB to include

        Returns:
            List of dictionaries with index usage info
        """
        result = self.session.execute(
            text(
                """
                SELECT
                    schemaname,
                    relname AS table_name,
                    indexrelname AS index_name,
                    idx_scan AS scans,
                    idx_tup_read AS tuples_read,
                    idx_tup_fetch AS tuples_fetched,
                    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
                    pg_relation_size(indexrelid) / 1024.0 / 1024.0 AS size_mb
                FROM pg_stat_user_indexes
                WHERE pg_relation_size(indexrelid) > :min_size_bytes
                ORDER BY idx_scan ASC, pg_relation_size(indexrelid) DESC
                """
            ),
            {"min_size_bytes": int(min_size_mb * 1024 * 1024)},
        )

        return [
            {
                "schema": row.schemaname,
                "table_name": row.table_name,
                "index_name": row.index_name,
                "scans": row.scans,
                "tuples_read": row.tuples_read,
                "tuples_fetched": row.tuples_fetched,
                "index_size": row.index_size,
                "size_mb": float(row.size_mb),
            }
            for row in result
        ]

    def reset_stats(self) -> bool:
        """Reset pg_stat_statements statistics.

        Useful after deploying optimizations to measure impact.

        Returns:
            True if reset was successful, False otherwise
        """
        if not self.is_pg_stat_statements_enabled():
            logger.warning("pg_stat_statements not enabled")
            return False

        try:
            self.session.execute(text("SELECT pg_stat_statements_reset()"))
            self.session.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to reset pg_stat_statements: {e}")
            return False

    def get_database_size(self) -> dict[str, Any]:
        """Get database size information.

        Returns:
            Dictionary with database size stats
        """
        result = self.session.execute(
            text(
                """
                SELECT
                    pg_size_pretty(pg_database_size(current_database())) AS database_size,
                    pg_database_size(current_database()) AS size_bytes,
                    (SELECT count(*) FROM pg_stat_user_tables) AS table_count,
                    (SELECT count(*) FROM pg_stat_user_indexes) AS index_count
                """
            )
        )
        row = result.fetchone()
        if row:
            return {
                "database_size": row.database_size,
                "size_bytes": row.size_bytes,
                "table_count": row.table_count,
                "index_count": row.index_count,
            }
        return {}

    def analyze_table(self, table_name: str) -> bool:
        """Run ANALYZE on a specific table to update statistics.

        Args:
            table_name: Name of the table to analyze

        Returns:
            True if successful, False otherwise
        """
        try:
            # Validate table name to prevent SQL injection
            result = self.session.execute(
                text("SELECT 1 FROM pg_stat_user_tables WHERE relname = :table_name"),
                {"table_name": table_name},
            )
            if not result.scalar():
                logger.warning(f"Table {table_name} not found")
                return False

            self.session.execute(text(f'ANALYZE "{table_name}"'))
            self.session.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to analyze table {table_name}: {e}")
            return False

    def generate_report(self) -> dict[str, Any]:
        """Generate a comprehensive database health report.

        Returns:
            Dictionary with all monitoring data
        """
        report: dict[str, Any] = {
            "is_postgres": self.is_postgres(),
            "pg_stat_statements_enabled": False,
            "database_size": {},
            "connection_stats": {},
            "slowest_queries": [],
            "most_frequent_queries": [],
            "slow_average_queries": [],
            "missing_fk_indexes": [],
            "tables_needing_analyze": [],
        }

        if not report["is_postgres"]:
            return report

        report["pg_stat_statements_enabled"] = self.is_pg_stat_statements_enabled()
        report["database_size"] = self.get_database_size()
        report["connection_stats"] = self.get_connection_stats()

        if report["pg_stat_statements_enabled"]:
            report["slowest_queries"] = [vars(q) for q in self.get_slowest_queries(limit=5)]
            report["most_frequent_queries"] = [
                vars(q) for q in self.get_most_frequent_queries(limit=5)
            ]
            report["slow_average_queries"] = [
                vars(q) for q in self.get_slow_average_queries(limit=5)
            ]

        report["missing_fk_indexes"] = [vars(idx) for idx in self.find_missing_fk_indexes()]

        # Tables that haven't been analyzed recently
        table_stats = self.get_table_stats()
        report["tables_needing_analyze"] = [
            vars(t)
            for t in table_stats
            if t.last_analyze is None or t.dead_tuples > t.live_tuples * 0.1
        ]

        return report
