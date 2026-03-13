"""Tests for aspect contracts — registry, envelope, validation (Issue #2929)."""

import json

import pytest

from nexus.contracts.aspects import (
    MAX_ASPECT_PAYLOAD_BYTES,
    AspectBase,
    AspectEnvelope,
    AspectRegistry,
    OwnershipAspect,
    PathAspect,
    SchemaMetadataAspect,
    register_aspect,
)


class TestAspectEnvelope:
    """AspectEnvelope value type tests."""

    def test_create_envelope(self) -> None:
        env = AspectEnvelope(
            aspect_name="test",
            version=0,
            payload={"key": "value"},
        )
        assert env.aspect_name == "test"
        assert env.version == 0
        assert env.payload == {"key": "value"}

    def test_to_json(self) -> None:
        env = AspectEnvelope(
            aspect_name="test",
            version=0,
            payload={"name": "col1", "type": "string"},
        )
        parsed = json.loads(env.to_json())
        assert parsed["name"] == "col1"

    def test_from_json(self) -> None:
        env = AspectEnvelope.from_json(
            aspect_name="test",
            version=1,
            json_str='{"name": "col1"}',
        )
        assert env.aspect_name == "test"
        assert env.version == 1
        assert env.payload["name"] == "col1"

    def test_frozen(self) -> None:
        env = AspectEnvelope(aspect_name="t", version=0, payload={})
        with pytest.raises(AttributeError):
            env.aspect_name = "other"


class TestAspectRegistry:
    """AspectRegistry singleton + validation tests."""

    def setup_method(self) -> None:
        AspectRegistry.reset()
        # Re-register built-in aspects (reset clears them)
        # The module-level decorators run at import time, so we need
        # to manually register for tests after reset
        registry = AspectRegistry.get()
        registry.register("path", PathAspect, max_versions=5)
        registry.register("schema_metadata", SchemaMetadataAspect, max_versions=20)
        registry.register("ownership", OwnershipAspect, max_versions=5)

    def test_singleton(self) -> None:
        r1 = AspectRegistry.get()
        r2 = AspectRegistry.get()
        assert r1 is r2

    def test_built_in_aspects_registered(self) -> None:
        registry = AspectRegistry.get()
        assert registry.is_registered("path")
        assert registry.is_registered("schema_metadata")
        assert registry.is_registered("ownership")

    def test_list_aspects(self) -> None:
        registry = AspectRegistry.get()
        names = registry.list_aspects()
        assert "path" in names
        assert "schema_metadata" in names

    def test_max_versions(self) -> None:
        registry = AspectRegistry.get()
        assert registry.max_versions_for("path") == 5
        assert registry.max_versions_for("schema_metadata") == 20

    def test_validate_payload_unknown_aspect(self) -> None:
        registry = AspectRegistry.get()
        with pytest.raises(ValueError, match="Unknown aspect type"):
            registry.validate_payload("nonexistent", {"key": "val"})

    def test_validate_payload_too_large(self) -> None:
        registry = AspectRegistry.get()
        big_payload = {"data": "x" * (MAX_ASPECT_PAYLOAD_BYTES + 1)}
        with pytest.raises(ValueError, match="exceeds"):
            registry.validate_payload("path", big_payload)

    def test_validate_payload_valid(self) -> None:
        registry = AspectRegistry.get()
        # Should not raise
        registry.validate_payload("path", {"virtual_path": "/test"})

    def test_register_duplicate_same_class_ok(self) -> None:
        registry = AspectRegistry.get()
        # Re-registering same name+class is OK
        registry.register("path", PathAspect)

    def test_register_duplicate_different_class_raises(self) -> None:
        registry = AspectRegistry.get()
        with pytest.raises(ValueError, match="already registered"):
            registry.register("path", SchemaMetadataAspect)

    def test_decorator_registers(self) -> None:
        AspectRegistry.reset()
        registry = AspectRegistry.get()

        @register_aspect("test_aspect_42", max_versions=10)
        class TestAspect(AspectBase):
            pass

        assert registry.is_registered("test_aspect_42")
        assert registry.max_versions_for("test_aspect_42") == 10


class TestBuiltInAspects:
    """Test built-in aspect classes."""

    def test_path_aspect_to_dict(self) -> None:
        aspect = PathAspect(virtual_path="/data/file.csv", backend_id="s3")
        d = aspect.to_dict()
        assert d["virtual_path"] == "/data/file.csv"
        assert d["backend_id"] == "s3"

    def test_schema_metadata_aspect(self) -> None:
        aspect = SchemaMetadataAspect(
            columns=[{"name": "id", "type": "integer"}],
            format="csv",
            row_count=100,
            confidence=0.95,
        )
        d = aspect.to_dict()
        assert d["format"] == "csv"
        assert d["row_count"] == 100
        assert len(d["columns"]) == 1

    def test_ownership_aspect(self) -> None:
        aspect = OwnershipAspect(owner_id="alice", owner_type="user")
        d = aspect.to_dict()
        assert d["owner_id"] == "alice"
