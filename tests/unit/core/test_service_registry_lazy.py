"""Unit tests for ServiceRegistry.register_factory() (lazy construction)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.service_registry import ServiceRef, ServiceRegistry


@pytest.fixture()
def registry() -> ServiceRegistry:
    return ServiceRegistry()


class TestRegisterFactory:
    """Tests for register_factory() lazy construction."""

    def test_factory_not_called_on_register(self, registry: ServiceRegistry) -> None:
        """Factory function is NOT called at register_factory() time."""
        factory = MagicMock(return_value=MagicMock())
        registry.register_factory("lazy_svc", factory)
        factory.assert_not_called()

    def test_factory_called_on_first_access(self, registry: ServiceRegistry) -> None:
        """Factory function IS called on first service() lookup."""
        instance = MagicMock()
        factory = MagicMock(return_value=instance)
        registry.register_factory("lazy_svc", factory)

        ref = registry.service("lazy_svc")
        assert ref is not None
        assert isinstance(ref, ServiceRef)
        assert ref._service_instance is instance
        factory.assert_called_once()

    def test_factory_called_only_once(self, registry: ServiceRegistry) -> None:
        """Second service() call returns cached result, no second factory call."""
        instance = MagicMock()
        factory = MagicMock(return_value=instance)
        registry.register_factory("lazy_svc", factory)

        ref1 = registry.service("lazy_svc")
        ref2 = registry.service("lazy_svc")
        assert ref1 is not None
        assert ref2 is not None
        assert ref1._service_instance is ref2._service_instance
        factory.assert_called_once()

    def test_factory_miss_returns_none(self, registry: ServiceRegistry) -> None:
        """service() on unregistered name still returns None."""
        assert registry.service("nonexistent") is None

    def test_factory_with_kwargs(self, registry: ServiceRegistry) -> None:
        """register_factory forwards kwargs to register_service."""
        instance = MagicMock(spec=["glob"])
        factory = MagicMock(return_value=instance)
        registry.register_factory("lazy_svc", factory, exports=("glob",))

        ref = registry.service("lazy_svc")
        assert ref is not None
        info = registry.service_info("lazy_svc")
        assert info is not None
        assert info.exports == ("glob",)

    def test_factory_overridden_by_direct_register(self, registry: ServiceRegistry) -> None:
        """Direct register_service before first access overrides factory."""
        factory = MagicMock(return_value=MagicMock())
        registry.register_factory("lazy_svc", factory)

        direct_instance = MagicMock()
        registry.register_service("lazy_svc", direct_instance)

        ref = registry.service("lazy_svc")
        assert ref is not None
        assert ref._service_instance is direct_instance
        factory.assert_not_called()
