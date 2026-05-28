"""Per-finalizer unit tests (Issue #2061).

Tests each concrete zone finalizer in isolation:
- SearchZoneFinalizer: bulk entity/relationship deletion
- ReBACZoneFinalizer: bulk tuple deletion
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from nexus.services.lifecycle.zone_finalizers.rebac_finalizer import ReBACZoneFinalizer
from nexus.services.lifecycle.zone_finalizers.search_finalizer import SearchZoneFinalizer

# ---------------------------------------------------------------------------
# SearchZoneFinalizer
# ---------------------------------------------------------------------------


class TestSearchZoneFinalizer:
    def test_finalizer_key(self):
        f = SearchZoneFinalizer(session_factory=MagicMock())
        assert f.finalizer_key == "nexus.core/search"

    @pytest.mark.asyncio
    async def test_bulk_deletes_entities_and_relationships(self):
        mock_session = MagicMock()
        mock_result_entities = MagicMock()
        mock_result_entities.rowcount = 5
        mock_result_rels = MagicMock()
        mock_result_rels.rowcount = 3
        mock_session.execute.side_effect = [mock_result_entities, mock_result_rels]

        @contextmanager
        def factory():
            yield mock_session

        f = SearchZoneFinalizer(session_factory=factory)

        await f.finalize_zone("zone-1")

        assert mock_session.execute.call_count == 2
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_zone_no_error(self):
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute.return_value = mock_result

        @contextmanager
        def factory():
            yield mock_session

        f = SearchZoneFinalizer(session_factory=factory)
        await f.finalize_zone("empty-zone")  # Should not raise


# ---------------------------------------------------------------------------
# ReBACZoneFinalizer
# ---------------------------------------------------------------------------


class TestReBACZoneFinalizer:
    def test_finalizer_key(self):
        f = ReBACZoneFinalizer(session_factory=MagicMock())
        assert f.finalizer_key == "nexus.core/rebac"

    @pytest.mark.asyncio
    async def test_bulk_deletes_tuples(self):
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 100
        mock_session.execute.return_value = mock_result

        @contextmanager
        def factory():
            yield mock_session

        f = ReBACZoneFinalizer(session_factory=factory)

        await f.finalize_zone("zone-1")

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_zone_no_error(self):
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute.return_value = mock_result

        @contextmanager
        def factory():
            yield mock_session

        f = ReBACZoneFinalizer(session_factory=factory)
        await f.finalize_zone("empty-zone")  # Should not raise


# ---------------------------------------------------------------------------
