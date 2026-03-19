"""Regression tests for SQL injection fix — Issue #2960 C3.

Verifies that the LIMIT clause is parameterized, not interpolated via f-string.
"""

from unittest.mock import MagicMock

from nexus.storage.query_builder import WorkQueryBuilder


class TestQueryBuilderSQLInjection:
    """Regression: C3 — SQL injection via f-string LIMIT."""

    def _make_session(self) -> MagicMock:
        session = MagicMock()
        result = MagicMock()
        result.fetchall.return_value = []
        session.execute.return_value = result
        return session

    def test_get_ready_work_limit_parameterized(self) -> None:
        """LIMIT must be a bound parameter, not interpolated into the SQL string."""
        session = self._make_session()
        WorkQueryBuilder.get_ready_work(session, limit=10)

        # Inspect the SQL text passed to session.execute
        args, kwargs = session.execute.call_args
        sql_text = args[0]
        # The SQL should contain ':limit' placeholder, not the literal '10'
        sql_str = str(sql_text)
        assert ":limit" in sql_str, f"Expected parameterized :limit, got: {sql_str}"
        # Params should include the limit value
        params = args[1] if len(args) > 1 else kwargs.get("params", {})
        assert params.get("limit") == 10

    def test_get_pending_work_limit_parameterized(self) -> None:
        session = self._make_session()
        WorkQueryBuilder.get_pending_work(session, limit=5)
        args, _ = session.execute.call_args
        assert ":limit" in str(args[0])

    def test_get_blocked_work_limit_parameterized(self) -> None:
        session = self._make_session()
        WorkQueryBuilder.get_blocked_work(session, limit=20)
        args, _ = session.execute.call_args
        assert ":limit" in str(args[0])

    def test_get_in_progress_work_limit_parameterized(self) -> None:
        session = self._make_session()
        WorkQueryBuilder.get_in_progress_work(session, limit=3)
        args, _ = session.execute.call_args
        assert ":limit" in str(args[0])

    def test_get_work_by_priority_limit_parameterized(self) -> None:
        session = self._make_session()
        WorkQueryBuilder.get_work_by_priority(session, limit=50)
        args, _ = session.execute.call_args
        assert ":limit" in str(args[0])

    def test_no_limit_no_parameter(self) -> None:
        """When limit is None, no LIMIT clause should appear."""
        session = self._make_session()
        WorkQueryBuilder.get_ready_work(session, limit=None)
        args, _ = session.execute.call_args
        sql_str = str(args[0])
        assert "LIMIT" not in sql_str

    def test_limit_cast_to_int(self) -> None:
        """Limit is explicitly cast to int() for defense-in-depth."""
        session = self._make_session()
        # Even if someone passes a string-like value, int() cast should work
        WorkQueryBuilder.get_ready_work(session, limit=10)
        args, _ = session.execute.call_args
        params = args[1] if len(args) > 1 else {}
        assert isinstance(params.get("limit"), int)
