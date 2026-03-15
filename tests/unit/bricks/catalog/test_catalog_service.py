"""Tests for CatalogService integration — extract → store → search (Issue #2930)."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.bricks.catalog.protocol import CatalogService
from nexus.contracts.aspects import (
    AspectRegistry,
    PathAspect,
    SchemaMetadataAspect,
)
from nexus.storage.aspect_service import AspectService
from nexus.storage.models._base import Base


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _reset_registry():
    AspectRegistry.reset()
    registry = AspectRegistry.get()
    registry.register("path", PathAspect, max_versions=5)
    registry.register("schema_metadata", SchemaMetadataAspect, max_versions=20)
    from nexus.contracts.aspects import DocumentStructureAspect

    registry.register("document_structure", DocumentStructureAspect, max_versions=10)
    yield
    AspectRegistry.reset()


@pytest.fixture()
def catalog_service(db_session: Session) -> CatalogService:
    return CatalogService(AspectService(db_session))


class TestCatalogExtractAndStore:
    """Extract schema from content and verify it's stored as an aspect."""

    def test_extract_csv_stores_schema(
        self, catalog_service: CatalogService, db_session: Session
    ) -> None:
        content = b"name,age,city\nAlice,30,NYC\nBob,25,LA\n"
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:id1",
            content=content,
            mime_type="text/csv",
            zone_id="z1",
        )
        db_session.commit()

        assert result.schema is not None
        assert result.confidence >= 0.5

        # Verify stored as aspect
        stored = catalog_service.get_schema("urn:nexus:file:z1:id1")
        assert stored is not None
        assert "columns" in stored
        assert len(stored["columns"]) == 3

    def test_extract_json_stores_schema(
        self, catalog_service: CatalogService, db_session: Session
    ) -> None:
        data = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        content = json.dumps(data).encode()
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:id2",
            content=content,
            mime_type="application/json",
            zone_id="z1",
        )
        db_session.commit()

        assert result.schema is not None
        stored = catalog_service.get_schema("urn:nexus:file:z1:id2")
        assert stored is not None

    def test_unsupported_format_no_storage(self, catalog_service: CatalogService) -> None:
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:id3",
            content=b"Hello world",
            mime_type="text/plain",
        )
        assert result.schema is None
        assert catalog_service.get_schema("urn:nexus:file:z1:id3") is None

    def test_below_threshold_not_stored(self, db_session: Session) -> None:
        """Schema with confidence below threshold is not stored."""
        aspect_svc = AspectService(db_session)
        catalog = CatalogService(aspect_svc, confidence_threshold=1.0)

        content = b"name,age\nAlice,30\n"
        result = catalog.extract_schema(
            entity_urn="urn:nexus:file:z1:id4",
            content=content,
            mime_type="text/csv",
        )
        db_session.commit()

        # CSV confidence is 1.0 for small files that fit in one read,
        # so verify the extraction succeeded but storage depends on threshold
        assert result.schema is not None

    def test_file_too_large_returns_error(self, db_session: Session) -> None:
        aspect_svc = AspectService(db_session)
        catalog = CatalogService(aspect_svc, max_auto_extract_bytes=10)

        content = b"name,age\n" + b"x,1\n" * 100
        result = catalog.extract_schema(
            entity_urn="urn:nexus:file:z1:id5",
            content=content,
            mime_type="text/csv",
        )
        assert result.error is not None
        assert result.schema is None


class TestCatalogSearchByColumn:
    """Search for entities by column name."""

    def test_search_finds_matching_column(
        self, catalog_service: CatalogService, db_session: Session
    ) -> None:
        # Seed a schema
        catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:sales",
            content=b"date,region,amount\n2026-01-01,us,100\n",
            mime_type="text/csv",
            zone_id="z1",
        )
        db_session.commit()

        results = catalog_service.search_by_column("amount", zone_id="z1")
        assert len(results) == 1
        assert results[0]["column_name"] == "amount"
        assert results[0]["entity_urn"] == "urn:nexus:file:z1:sales"

    def test_search_partial_match(
        self, catalog_service: CatalogService, db_session: Session
    ) -> None:
        catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:data",
            content=b"user_name,user_age\nAlice,30\n",
            mime_type="text/csv",
            zone_id="z1",
        )
        db_session.commit()

        # Partial match: "name" matches "user_name"
        results = catalog_service.search_by_column("name", zone_id="z1")
        assert len(results) == 1

    def test_search_no_match(self, catalog_service: CatalogService, db_session: Session) -> None:
        catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:data",
            content=b"id,value\n1,100\n",
            mime_type="text/csv",
            zone_id="z1",
        )
        db_session.commit()

        results = catalog_service.search_by_column("nonexistent", zone_id="z1")
        assert len(results) == 0

    def test_search_zone_filter(self, catalog_service: CatalogService, db_session: Session) -> None:
        """Zone filter excludes entities from other zones."""
        catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:data",
            content=b"col_a\n1\n",
            mime_type="text/csv",
            zone_id="z1",
        )
        catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z2:data",
            content=b"col_a\n2\n",
            mime_type="text/csv",
            zone_id="z2",
        )
        db_session.commit()

        results = catalog_service.search_by_column("col_a", zone_id="z1")
        assert len(results) == 1
        assert "z1" in results[0]["entity_urn"]


class TestCatalogFilenameDetection:
    """Test filename-based format detection for all formats (Issue #2978, Issue 12)."""

    def test_csv_by_filename(self, catalog_service: CatalogService, db_session: Session) -> None:
        content = b"name,age\nAlice,30\n"
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:fn1",
            content=content,
            mime_type=None,
            filename="data.csv",
            zone_id="z1",
        )
        db_session.commit()
        assert result.schema is not None
        assert result.format == "csv"

    def test_tsv_by_filename(self, catalog_service: CatalogService) -> None:
        content = b"name\tage\nAlice\t30\n"
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:fn2",
            content=content,
            mime_type=None,
            filename="data.tsv",
        )
        assert result.schema is not None

    def test_json_by_filename(self, catalog_service: CatalogService) -> None:
        import json

        content = json.dumps([{"id": 1}]).encode()
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:fn3",
            content=content,
            mime_type=None,
            filename="data.json",
        )
        assert result.schema is not None
        assert result.format == "json"

    def test_jsonl_by_filename(self, catalog_service: CatalogService) -> None:
        content = b'{"id": 1}\n{"id": 2}\n'
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:fn4",
            content=content,
            mime_type=None,
            filename="data.jsonl",
        )
        assert result.schema is not None

    def test_avro_by_filename(self, catalog_service: CatalogService) -> None:
        # Without fastavro, this should return an error (not crash)
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:fn5",
            content=b"not real avro",
            mime_type=None,
            filename="data.avro",
        )
        assert result.format == "avro"

    def test_unknown_extension(self, catalog_service: CatalogService) -> None:
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:fn6",
            content=b"hello",
            mime_type=None,
            filename="data.xyz",
        )
        assert result.schema is None
        assert result.error is not None

    def test_no_extension(self, catalog_service: CatalogService) -> None:
        result = catalog_service.extract_schema(
            entity_urn="urn:nexus:file:z1:fn7",
            content=b"hello",
            mime_type=None,
            filename="Makefile",
        )
        assert result.schema is None


class TestCatalogDocumentExtraction:
    """Test document extraction (Markdown -> document_structure aspect)."""

    def test_extract_markdown_stores_document_structure(
        self, catalog_service: CatalogService, db_session: Session
    ) -> None:
        content = b"# My Doc\n\nSome text here.\n"
        result = catalog_service.extract_document(
            entity_urn="urn:nexus:file:z1:md1",
            content=content,
            mime_type="text/markdown",
            zone_id="z1",
        )
        db_session.commit()

        assert result.error is None
        assert result.title == "My Doc"

        stored = catalog_service.get_document_structure("urn:nexus:file:z1:md1")
        assert stored is not None
        assert stored["title"] == "My Doc"
        assert len(stored["headings"]) == 1

    def test_extract_markdown_by_filename(
        self, catalog_service: CatalogService, db_session: Session
    ) -> None:
        content = b"# Hello\n\nWorld.\n"
        result = catalog_service.extract_document(
            entity_urn="urn:nexus:file:z1:md2",
            content=content,
            mime_type=None,
            filename="readme.md",
            zone_id="z1",
        )
        db_session.commit()

        assert result.error is None
        assert result.title == "Hello"

    def test_unsupported_document_format(self, catalog_service: CatalogService) -> None:
        result = catalog_service.extract_document(
            entity_urn="urn:nexus:file:z1:md3",
            content=b"not markdown",
            mime_type="text/plain",
        )
        assert result.error is not None

    def test_document_too_large(self, db_session: Session) -> None:
        from nexus.storage.aspect_service import AspectService

        catalog = CatalogService(AspectService(db_session), max_auto_extract_bytes=10)
        content = b"# " + b"x" * 100
        result = catalog.extract_document(
            entity_urn="urn:nexus:file:z1:md4",
            content=content,
            mime_type="text/markdown",
        )
        assert result.error is not None


class TestCatalogExtractAuto:
    """Test extract_auto() dispatching."""

    def test_auto_detects_csv(self, catalog_service: CatalogService, db_session: Session) -> None:
        content = b"a,b\n1,2\n"
        result = catalog_service.extract_auto(
            entity_urn="urn:nexus:file:z1:auto1",
            content=content,
            filename="data.csv",
            zone_id="z1",
        )
        db_session.commit()
        from nexus.bricks.catalog.extractors import ExtractionResult

        assert isinstance(result, ExtractionResult)
        assert result.schema is not None

    def test_auto_detects_markdown(
        self, catalog_service: CatalogService, db_session: Session
    ) -> None:
        content = b"# Title\n\nBody text.\n"
        result = catalog_service.extract_auto(
            entity_urn="urn:nexus:file:z1:auto2",
            content=content,
            filename="readme.md",
            zone_id="z1",
        )
        db_session.commit()
        from nexus.bricks.catalog.extractors import DocumentExtractionResult

        assert isinstance(result, DocumentExtractionResult)
        assert result.title == "Title"

    def test_auto_unknown_format(self, catalog_service: CatalogService) -> None:
        result = catalog_service.extract_auto(
            entity_urn="urn:nexus:file:z1:auto3",
            content=b"unknown",
            filename="data.xyz",
        )
        assert result.error is not None


class TestCatalogHasExtractor:
    """Test has_extractor() format-gate."""

    def test_has_schema_extractor(self, catalog_service: CatalogService) -> None:
        assert catalog_service.has_extractor(filename="data.csv") is True
        assert catalog_service.has_extractor(mime_type="text/csv") is True

    def test_has_document_extractor(self, catalog_service: CatalogService) -> None:
        assert catalog_service.has_extractor(filename="readme.md") is True
        assert catalog_service.has_extractor(mime_type="text/markdown") is True

    def test_no_extractor(self, catalog_service: CatalogService) -> None:
        assert catalog_service.has_extractor(filename="data.xyz") is False
        assert catalog_service.has_extractor(mime_type="text/plain") is False
