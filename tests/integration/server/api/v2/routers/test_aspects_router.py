"""Tests for aspects REST API router (Issue #2930)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.aspects import (
    AspectRegistry,
    OwnershipAspect,
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
    registry.register("ownership", OwnershipAspect, max_versions=5)
    yield
    AspectRegistry.reset()


class TestAspectServiceViaRouter:
    """Test aspect operations that the router would exercise.

    These test the service layer directly (not HTTP) since setting up
    a full FastAPI TestClient with all dependencies is complex.
    They validate the same code paths the router calls.
    """

    def test_list_aspects_empty(self, db_session) -> None:
        svc = AspectService(db_session)
        assert svc.list_aspects("urn:nexus:file:z1:missing") == []

    def test_list_aspects_multiple(self, db_session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/a"})
        svc.put_aspect(
            "urn:nexus:file:z1:id1", "ownership", {"owner_id": "alice", "owner_type": "user"}
        )
        db_session.commit()

        names = svc.list_aspects("urn:nexus:file:z1:id1")
        assert sorted(names) == ["ownership", "path"]

    def test_get_aspect_current(self, db_session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/a"}, zone_id="z1")
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None
        assert result["virtual_path"] == "/a"

    def test_get_aspect_specific_version(self, db_session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v1"})
        db_session.commit()
        v = svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/v2"})
        db_session.commit()

        result = svc.get_aspect_version("urn:nexus:file:z1:id1", "path", v)
        assert result is not None
        assert result["virtual_path"] == "/v1"

    def test_get_aspect_not_found(self, db_session) -> None:
        svc = AspectService(db_session)
        assert svc.get_aspect("urn:nexus:file:z1:nope", "path") is None

    def test_put_aspect_creates(self, db_session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect(
            "urn:nexus:file:z1:id1",
            "path",
            {"virtual_path": "/new"},
            zone_id="z1",
        )
        db_session.commit()

        result = svc.get_aspect("urn:nexus:file:z1:id1", "path")
        assert result is not None
        assert result["virtual_path"] == "/new"

    def test_put_aspect_validation_error(self, db_session) -> None:
        svc = AspectService(db_session)
        with pytest.raises(ValueError, match="Unknown aspect type"):
            svc.put_aspect("urn:nexus:file:z1:id1", "nonexistent", {"key": "val"})

    def test_delete_aspect_success(self, db_session) -> None:
        svc = AspectService(db_session)
        svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": "/a"})
        db_session.commit()

        assert svc.delete_aspect("urn:nexus:file:z1:id1", "path", zone_id="z1") is True
        db_session.commit()
        assert svc.get_aspect("urn:nexus:file:z1:id1", "path") is None

    def test_delete_aspect_not_found(self, db_session) -> None:
        svc = AspectService(db_session)
        assert svc.delete_aspect("urn:nexus:file:z1:nope", "path") is False

    def test_aspect_history(self, db_session) -> None:
        svc = AspectService(db_session)
        for i in range(3):
            svc.put_aspect("urn:nexus:file:z1:id1", "path", {"virtual_path": f"/v{i}"})
            db_session.commit()

        history = svc.get_aspect_history("urn:nexus:file:z1:id1", "path", limit=10)
        assert len(history) == 3

    def test_find_entities_with_limit(self, db_session) -> None:
        """Issue 14: find_entities_with_aspect respects limit."""
        svc = AspectService(db_session)
        for i in range(5):
            svc.put_aspect(f"urn:nexus:file:z1:id{i}", "path", {"virtual_path": f"/v{i}"})
        db_session.commit()

        result = svc.find_entities_with_aspect("path", limit=3)
        assert len(result) == 3

        result_all = svc.find_entities_with_aspect("path", limit=100)
        assert len(result_all) == 5
