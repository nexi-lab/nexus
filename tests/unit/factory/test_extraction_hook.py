"""Tests for async-on-write extraction hook (Issue #2978).

Verifies the factory-created extraction hook correctly:
- Format-gates: only extracts supported formats
- Size-gates: skips oversized files
- Reads content from the backend via content hash
- Calls CatalogService.extract_auto() with correct args
- Handles failures gracefully (best-effort)
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.aspects import (
    AspectRegistry,
    DocumentStructureAspect,
    PathAspect,
    SchemaMetadataAspect,
)
from nexus.factory._extraction_hook import make_extraction_hook
from nexus.storage.aspect_service import AspectService
from nexus.storage.models._base import Base


@pytest.fixture()
def db_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory


@pytest.fixture(autouse=True)
def _reset_registry():
    AspectRegistry.reset()
    registry = AspectRegistry.get()
    registry.register("path", PathAspect, max_versions=5)
    registry.register("schema_metadata", SchemaMetadataAspect, max_versions=20)
    registry.register("document_structure", DocumentStructureAspect, max_versions=10)
    yield
    AspectRegistry.reset()


class TestExtractionHook:
    """Test the make_extraction_hook factory function."""

    def test_extracts_csv_on_write(self, db_session_factory) -> None:
        """CSV write events trigger schema extraction."""
        mock_backend = MagicMock()
        mock_backend.read_content.return_value = b"name,age\nAlice,30\nBob,25\n"

        hook = make_extraction_hook(
            session_factory=db_session_factory,
            backend=mock_backend,
            metastore=MagicMock(),
        )

        events = [
            {
                "op": "write",
                "path": "/data/users.csv",
                "zone_id": "z1",
                "metadata": {
                    "content_id": "abc123",
                    "size": 100,
                    "mime_type": "text/csv",
                },
            }
        ]

        hook(events)

        # Verify backend was asked to read content
        mock_backend.read_content.assert_called_once_with("abc123")

        # Verify aspect was stored
        with db_session_factory() as session:
            svc = AspectService(session)
            from nexus.contracts.urn import NexusURN

            urn = str(NexusURN.for_file("z1", "/data/users.csv"))
            schema = svc.get_aspect(urn, "schema_metadata")
            assert schema is not None
            assert len(schema["columns"]) == 2

    def test_extracts_markdown_on_write(self, db_session_factory) -> None:
        """Markdown write events trigger document extraction."""
        mock_backend = MagicMock()
        mock_backend.read_content.return_value = b"# My Doc\n\nSome text.\n"

        hook = make_extraction_hook(
            session_factory=db_session_factory,
            backend=mock_backend,
            metastore=MagicMock(),
        )

        events = [
            {
                "op": "write",
                "path": "/docs/readme.md",
                "zone_id": "z1",
                "metadata": {
                    "content_id": "md123",
                    "size": 50,
                },
            }
        ]

        hook(events)

        with db_session_factory() as session:
            svc = AspectService(session)
            from nexus.contracts.urn import NexusURN

            urn = str(NexusURN.for_file("z1", "/docs/readme.md"))
            doc = svc.get_aspect(urn, "document_structure")
            assert doc is not None
            assert doc["title"] == "My Doc"

    def test_skips_unsupported_format(self, db_session_factory) -> None:
        """Files with no registered extractor are skipped."""
        mock_backend = MagicMock()

        hook = make_extraction_hook(
            session_factory=db_session_factory,
            backend=mock_backend,
            metastore=MagicMock(),
        )

        events = [
            {
                "op": "write",
                "path": "/code/main.py",
                "zone_id": "z1",
                "metadata": {"content_id": "py123", "size": 50},
            }
        ]

        hook(events)

        # Backend should NOT be called — format-gated
        mock_backend.read_content.assert_not_called()

    def test_skips_oversized_files(self, db_session_factory) -> None:
        """Files exceeding max_extract_bytes are skipped."""
        mock_backend = MagicMock()

        hook = make_extraction_hook(
            session_factory=db_session_factory,
            backend=mock_backend,
            metastore=MagicMock(),
            max_extract_bytes=100,
        )

        events = [
            {
                "op": "write",
                "path": "/data/big.csv",
                "zone_id": "z1",
                "metadata": {"content_id": "big123", "size": 200, "mime_type": "text/csv"},
            }
        ]

        hook(events)

        mock_backend.read_content.assert_not_called()

    def test_skips_non_write_events(self, db_session_factory) -> None:
        """Delete, rename, mkdir events are ignored."""
        mock_backend = MagicMock()

        hook = make_extraction_hook(
            session_factory=db_session_factory,
            backend=mock_backend,
            metastore=MagicMock(),
        )

        events = [
            {"op": "delete", "path": "/data/old.csv", "zone_id": "z1"},
            {"op": "mkdir", "path": "/data/new", "zone_id": "z1"},
        ]

        hook(events)

        mock_backend.read_content.assert_not_called()

    def test_skips_missing_etag(self, db_session_factory) -> None:
        """Events without content_id (content hash) are skipped."""
        mock_backend = MagicMock()

        hook = make_extraction_hook(
            session_factory=db_session_factory,
            backend=mock_backend,
            metastore=MagicMock(),
        )

        events = [
            {
                "op": "write",
                "path": "/data/file.csv",
                "zone_id": "z1",
                "metadata": {"size": 50, "mime_type": "text/csv"},
            }
        ]

        hook(events)

        mock_backend.read_content.assert_not_called()

    def test_backend_failure_does_not_raise(self, db_session_factory) -> None:
        """Backend read failure is logged, not raised."""
        mock_backend = MagicMock()
        mock_backend.read_content.side_effect = RuntimeError("Backend down")

        hook = make_extraction_hook(
            session_factory=db_session_factory,
            backend=mock_backend,
            metastore=MagicMock(),
        )

        events = [
            {
                "op": "write",
                "path": "/data/file.csv",
                "zone_id": "z1",
                "metadata": {"content_id": "fail123", "size": 50, "mime_type": "text/csv"},
            }
        ]

        # Should not raise
        hook(events)

    def test_batch_extraction_partial_failure(self, db_session_factory) -> None:
        """One file failing doesn't prevent other files from being extracted."""
        mock_backend = MagicMock()

        def read_content(content_id):
            if content_id == "bad":
                raise RuntimeError("Corrupted")
            return b"name,age\nAlice,30\n"

        mock_backend.read_content.side_effect = read_content

        hook = make_extraction_hook(
            session_factory=db_session_factory,
            backend=mock_backend,
            metastore=MagicMock(),
        )

        events = [
            {
                "op": "write",
                "path": "/data/bad.csv",
                "zone_id": "z1",
                "metadata": {"content_id": "bad", "size": 50, "mime_type": "text/csv"},
            },
            {
                "op": "write",
                "path": "/data/good.csv",
                "zone_id": "z1",
                "metadata": {"content_id": "good", "size": 50, "mime_type": "text/csv"},
            },
        ]

        hook(events)

        # Good file should still be extracted
        with db_session_factory() as session:
            svc = AspectService(session)
            from nexus.contracts.urn import NexusURN

            urn = str(NexusURN.for_file("z1", "/data/good.csv"))
            schema = svc.get_aspect(urn, "schema_metadata")
            assert schema is not None
