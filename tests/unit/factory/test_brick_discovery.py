"""Tests for brick auto-discovery — Issue #2180."""

from nexus.contracts.brick_manifest import BrickManifest
from nexus.factory._bricks import BrickFactoryDescriptor, _discover_brick_factories


class TestBrickAutoDiscovery:
    """Tests for _discover_brick_factories()."""

    def test_discovers_delegation_brick(self) -> None:
        factories = _discover_brick_factories("independent")
        keys = {f.result_key for f in factories}
        assert "delegation_service" in keys

    def test_removed_bricks_absent(self) -> None:
        """Verify bricks removed in #2988 are no longer discovered."""
        factories = _discover_brick_factories("independent")
        keys = {f.result_key for f in factories}
        assert "reputation_service" not in keys
        assert "tools_service" not in keys

    def test_descriptor_fields(self) -> None:
        factories = _discover_brick_factories("independent")
        delegation = next(f for f in factories if f.result_key == "delegation_service")
        assert delegation.name is None  # No profile gate
        assert callable(delegation.create_fn)

    def test_descriptor_manifest_none_when_absent(self) -> None:
        """Bricks without MANIFEST attribute get manifest=None."""
        factories = _discover_brick_factories("independent")
        delegation = next(f for f in factories if f.result_key == "delegation_service")
        assert delegation.manifest is None


class TestBrickFactoryDescriptorManifest:
    """Tests for manifest integration in BrickFactoryDescriptor."""

    def test_descriptor_with_manifest(self) -> None:
        """Descriptor stores manifest reference."""
        manifest = BrickManifest(
            name="test",
            protocol="TestProtocol",
            required_modules=("os", "sys"),
        )
        desc = BrickFactoryDescriptor(
            name="test",
            result_key="test_service",
            create_fn=lambda ctx, system: None,
            manifest=manifest,
        )
        assert desc.manifest is manifest
        assert desc.manifest.all_required_present is True

    def test_descriptor_without_manifest(self) -> None:
        """Descriptor without manifest has None."""
        desc = BrickFactoryDescriptor(
            name="test",
            result_key="test_service",
            create_fn=lambda ctx, system: None,
        )
        assert desc.manifest is None

    def test_manifest_blocks_missing_modules(self) -> None:
        """Manifest with missing required module reports not present."""
        manifest = BrickManifest(
            name="test",
            protocol="TestProtocol",
            required_modules=("nexus.nonexistent.module.xyz",),
        )
        desc = BrickFactoryDescriptor(
            name="test",
            result_key="test_service",
            create_fn=lambda ctx, system: None,
            manifest=manifest,
        )
        assert desc.manifest.all_required_present is False
