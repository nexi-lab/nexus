"""Tests for storage views SQL generation."""

from nexus.storage.views import (
    _interval_ago,
    _json_extract,
    _now,
    get_blocked_work_view,
    get_in_progress_work_view,
    get_pending_work_view,
    get_ready_work_view,
    get_work_by_priority_view,
)


class TestHelperFunctions:
    """Test helper functions for SQL generation."""

    def test_json_extract_sqlite(self):
        """Test JSON extraction for SQLite."""
        result = _json_extract("metadata", "sqlite")
        assert "json_extract" in result
        assert "metadata" in result

    def test_json_extract_postgresql(self):
        """Test JSON extraction for PostgreSQL."""
        result = _json_extract("metadata", "postgresql")
        assert "::jsonb" in result
        assert "metadata" in result

    def test_now_sqlite(self):
        """Test NOW expression for SQLite."""
        result = _now("sqlite")
        assert result == "datetime('now')"

    def test_now_postgresql(self):
        """Test NOW expression for PostgreSQL."""
        result = _now("postgresql")
        assert result == "NOW()"

    def test_interval_ago_sqlite(self):
        """Test interval ago for SQLite."""
        result = _interval_ago("1 hour", "sqlite")
        assert "datetime('now'" in result
        assert "-1 hour" in result

    def test_interval_ago_sqlite_days(self):
        """Test interval ago for SQLite with days."""
        result = _interval_ago("7 days", "sqlite")
        assert "datetime('now'" in result
        assert "-7 days" in result

    def test_interval_ago_sqlite_invalid(self):
        """Test interval ago for SQLite with invalid format."""
        result = _interval_ago("invalid", "sqlite")
        assert result == "datetime('now')"

    def test_interval_ago_postgresql(self):
        """Test interval ago for PostgreSQL."""
        result = _interval_ago("1 hour", "postgresql")
        assert "NOW()" in result
        assert "INTERVAL '1 hour'" in result


class TestViewGeneration:
    """Test SQL view generation."""

    def test_get_ready_work_view_sqlite(self):
        """Test ready work view for SQLite."""
        view = get_ready_work_view("sqlite")
        assert view is not None
        sql = str(view)
        assert "ready_work_items" in sql.lower() or "select" in sql.lower()

    def test_get_ready_work_view_postgresql(self):
        """Test ready work view for PostgreSQL."""
        view = get_ready_work_view("postgresql")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()

    def test_get_pending_work_view_sqlite(self):
        """Test pending work view for SQLite."""
        view = get_pending_work_view("sqlite")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()

    def test_get_pending_work_view_postgresql(self):
        """Test pending work view for PostgreSQL."""
        view = get_pending_work_view("postgresql")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()

    def test_get_blocked_work_view_sqlite(self):
        """Test blocked work view for SQLite."""
        view = get_blocked_work_view("sqlite")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()

    def test_get_blocked_work_view_postgresql(self):
        """Test blocked work view for PostgreSQL."""
        view = get_blocked_work_view("postgresql")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()

    def test_get_in_progress_work_view_sqlite(self):
        """Test in-progress work view for SQLite."""
        view = get_in_progress_work_view("sqlite")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()

    def test_get_in_progress_work_view_postgresql(self):
        """Test in-progress work view for PostgreSQL."""
        view = get_in_progress_work_view("postgresql")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()

    def test_get_work_by_priority_view_sqlite(self):
        """Test work by priority view for SQLite."""
        view = get_work_by_priority_view("sqlite")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()

    def test_get_work_by_priority_view_postgresql(self):
        """Test work by priority view for PostgreSQL."""
        view = get_work_by_priority_view("postgresql")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()


class TestAdditionalViews:
    """Test additional view generation functions."""

    def test_get_ready_for_indexing_view_sqlite(self):
        """Test ready for indexing view for SQLite."""
        from nexus.storage.views import get_ready_for_indexing_view

        view = get_ready_for_indexing_view("sqlite")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()
        assert "file_paths" in sql.lower()

    def test_get_ready_for_indexing_view_postgresql(self):
        """Test ready for indexing view for PostgreSQL."""
        from nexus.storage.views import get_ready_for_indexing_view

        view = get_ready_for_indexing_view("postgresql")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()
        assert "create or replace view" in sql.lower()

    def test_get_hot_tier_eviction_view_sqlite(self):
        """Test hot tier eviction view for SQLite."""
        from nexus.storage.views import get_hot_tier_eviction_view

        view = get_hot_tier_eviction_view("sqlite")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()
        assert "hours_since_access" in sql.lower()

    def test_get_hot_tier_eviction_view_postgresql(self):
        """Test hot tier eviction view for PostgreSQL."""
        from nexus.storage.views import get_hot_tier_eviction_view

        view = get_hot_tier_eviction_view("postgresql")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()
        assert "extract" in sql.lower()

    def test_get_orphaned_content_view_sqlite(self):
        """Test orphaned content view for SQLite."""
        from nexus.storage.views import get_orphaned_content_view

        view = get_orphaned_content_view("sqlite")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()
        assert "days_since_access" in sql.lower()
        assert "content_chunks" in sql.lower()

    def test_get_orphaned_content_view_postgresql(self):
        """Test orphaned content view for PostgreSQL."""
        from nexus.storage.views import get_orphaned_content_view

        view = get_orphaned_content_view("postgresql")
        assert view is not None
        sql = str(view)
        assert "select" in sql.lower()
        assert "extract" in sql.lower()


class TestViewUtilities:
    """Test view utility functions."""

    def test_get_all_views_sqlite(self):
        """Test get_all_views for SQLite."""
        from nexus.storage.views import get_all_views

        views = get_all_views("sqlite")
        assert isinstance(views, list)
        assert len(views) > 0
        for name, view_sql in views:
            assert isinstance(name, str)
            assert view_sql is not None

    def test_get_all_views_postgresql(self):
        """Test get_all_views for PostgreSQL."""
        from nexus.storage.views import get_all_views

        views = get_all_views("postgresql")
        assert isinstance(views, list)
        assert len(views) > 0
        for name, view_sql in views:
            assert isinstance(name, str)
            assert view_sql is not None
            # PostgreSQL views should use CREATE OR REPLACE
            assert "create or replace view" in str(view_sql).lower()

    def test_view_generators_registry(self):
        """Test VIEW_GENERATORS registry."""
        from nexus.storage.views import VIEW_GENERATORS

        assert isinstance(VIEW_GENERATORS, list)
        assert len(VIEW_GENERATORS) > 0
        for name, func in VIEW_GENERATORS:
            assert isinstance(name, str)
            assert callable(func)

    def test_allowed_view_names(self):
        """Test ALLOWED_VIEW_NAMES security allowlist."""
        from nexus.storage.views import ALLOWED_VIEW_NAMES

        assert isinstance(ALLOWED_VIEW_NAMES, frozenset)
        assert len(ALLOWED_VIEW_NAMES) > 0
        assert "ready_work_items" in ALLOWED_VIEW_NAMES
        assert "pending_work_items" in ALLOWED_VIEW_NAMES
