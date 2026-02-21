"""Unit tests for BrickManifest base class (Issue #1386)."""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError, dataclass, field

import pytest

from nexus.contracts.brick_manifest import BrickManifest


class TestBrickManifestCreation:
    """Test manifest creation and field defaults."""

    def test_creation_with_required_fields(self) -> None:
        m = BrickManifest(name="test", protocol="TestProtocol")
        assert m.name == "test"
        assert m.protocol == "TestProtocol"
        assert m.version == "1.0.0"
        assert m.description == ""
        assert m.config_schema == {}
        assert m.dependencies == ()
        assert m.required_modules == ()
        assert m.optional_modules == ()

    def test_creation_with_all_fields(self) -> None:
        m = BrickManifest(
            name="search",
            protocol="SearchProtocol",
            version="2.0.0",
            description="Hybrid search brick",
            config_schema={"mode": {"type": "str", "default": "hybrid"}},
            dependencies=("memory",),
            required_modules=("nexus.bricks.search.query_service",),
            optional_modules=("nexus.bricks.search.bm25s_search",),
        )
        assert m.name == "search"
        assert m.version == "2.0.0"
        assert m.description == "Hybrid search brick"
        assert m.dependencies == ("memory",)
        assert len(m.required_modules) == 1
        assert len(m.optional_modules) == 1


class TestBrickManifestImmutability:
    """Frozen dataclass must reject attribute assignment."""

    def test_cannot_mutate_name(self) -> None:
        m = BrickManifest(name="test", protocol="P")
        with pytest.raises(FrozenInstanceError):
            m.name = "changed"

    def test_cannot_mutate_version(self) -> None:
        m = BrickManifest(name="test", protocol="P")
        with pytest.raises(FrozenInstanceError):
            m.version = "9.9.9"


class TestVerifyImports:
    """Test the verify_imports() method."""

    def test_all_required_present(self) -> None:
        """Modules from stdlib should always be importable."""
        m = BrickManifest(
            name="test",
            protocol="P",
            required_modules=("json", "os", "logging"),
        )
        results = m.verify_imports()
        assert results == {"json": True, "os": True, "logging": True}

    def test_required_missing_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        m = BrickManifest(
            name="test",
            protocol="P",
            required_modules=("nexus.nonexistent.fake_module",),
        )
        with caplog.at_level(logging.ERROR):
            results = m.verify_imports()
        assert results["nexus.nonexistent.fake_module"] is False
        assert "Required module missing for brick test" in caplog.text

    def test_optional_missing_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        m = BrickManifest(
            name="test",
            protocol="P",
            optional_modules=("nexus.nonexistent.optional_thing",),
        )
        with caplog.at_level(logging.WARNING):
            results = m.verify_imports()
        assert results["nexus.nonexistent.optional_thing"] is False
        assert "Optional module unavailable for brick test" in caplog.text

    def test_mixed_results(self) -> None:
        m = BrickManifest(
            name="test",
            protocol="P",
            required_modules=("json", "nexus.nonexistent.req"),
            optional_modules=("os", "nexus.nonexistent.opt"),
        )
        results = m.verify_imports()
        assert results["json"] is True
        assert results["nexus.nonexistent.req"] is False
        assert results["os"] is True
        assert results["nexus.nonexistent.opt"] is False

    def test_empty_modules_returns_empty_dict(self) -> None:
        m = BrickManifest(name="test", protocol="P")
        assert m.verify_imports() == {}


class TestAllRequiredPresent:
    """Test the all_required_present property."""

    def test_all_present(self) -> None:
        m = BrickManifest(name="test", protocol="P", required_modules=("json", "os"))
        assert m.all_required_present is True

    def test_one_missing(self) -> None:
        m = BrickManifest(
            name="test",
            protocol="P",
            required_modules=("json", "nexus.nonexistent.mod"),
        )
        assert m.all_required_present is False

    def test_empty_required(self) -> None:
        m = BrickManifest(name="test", protocol="P")
        assert m.all_required_present is True


class TestBrickManifestSubclass:
    """Test that brick-specific manifests can extend the base."""

    def test_subclass_inherits_fields(self) -> None:
        @dataclass(frozen=True)
        class ReBACManifest(BrickManifest):
            name: str = "rebac"
            protocol: str = "ReBACBrickProtocol"
            required_modules: tuple[str, ...] = ("json",)

        m = ReBACManifest()
        assert m.name == "rebac"
        assert m.protocol == "ReBACBrickProtocol"
        assert m.version == "1.0.0"  # inherited default
        assert m.required_modules == ("json",)

    def test_subclass_adds_extra_fields(self) -> None:
        @dataclass(frozen=True)
        class ExtendedManifest(BrickManifest):
            name: str = "ext"
            protocol: str = "ExtProtocol"
            custom_field: str = "extra"

        m = ExtendedManifest()
        assert m.custom_field == "extra"
        assert m.name == "ext"

    def test_subclass_config_schema(self) -> None:
        @dataclass(frozen=True)
        class SearchManifest(BrickManifest):
            name: str = "search"
            protocol: str = "SearchProtocol"
            config_schema: dict[str, dict[str, object]] = field(
                default_factory=lambda: {
                    "mode": {"type": "str", "default": "hybrid"},
                }
            )

        m = SearchManifest()
        assert "mode" in m.config_schema
        assert m.config_schema["mode"]["default"] == "hybrid"
