"""Unit tests for ProviderRegistry (Issue #2051).

Tests provider discovery, lazy initialization, and availability checks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.bricks.sandbox.provider_registry import ProviderRegistry
from nexus.bricks.sandbox.sandbox_provider import SandboxProvider


class FakeProvider(SandboxProvider):
    """Minimal SandboxProvider for testing."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name

    async def create(self, template_id=None, timeout_minutes=10, metadata=None, security_profile=None) -> str:
        return "fake-id"

    async def run_code(self, sandbox_id, language, code, timeout=300, as_script=False):
        return MagicMock(stdout="ok", stderr="", exit_code=0, execution_time=0.1)

    async def pause(self, sandbox_id: str) -> None:
        pass

    async def resume(self, sandbox_id: str) -> None:
        pass

    async def destroy(self, sandbox_id: str) -> None:
        pass

    async def get_info(self, sandbox_id: str):
        return MagicMock(sandbox_id=sandbox_id, status="active")

    async def is_available(self) -> bool:
        return True

    async def mount_nexus(self, sandbox_id, mount_path, nexus_url, api_key, agent_id=None, skip_dependency_checks=False):
        return {"success": True}


class TestProviderRegistration:
    """Tests for registering providers."""

    def test_register_and_get_provider(self):
        registry = ProviderRegistry()
        provider = FakeProvider("docker")
        registry.register("docker", provider)

        assert registry.get("docker") is provider

    def test_get_raises_for_unregistered(self):
        registry = ProviderRegistry()

        with pytest.raises(ValueError, match="not available"):
            registry.get("nonexistent")

    def test_get_error_message_lists_available(self):
        registry = ProviderRegistry()
        registry.register("docker", FakeProvider("docker"))

        with pytest.raises(ValueError, match="docker"):
            registry.get("e2b")

    def test_register_multiple_providers(self):
        registry = ProviderRegistry()
        docker = FakeProvider("docker")
        e2b = FakeProvider("e2b")

        registry.register("docker", docker)
        registry.register("e2b", e2b)

        assert registry.get("docker") is docker
        assert registry.get("e2b") is e2b

    def test_available_provider_names(self):
        registry = ProviderRegistry()
        registry.register("docker", FakeProvider("docker"))
        registry.register("monty", FakeProvider("monty"))

        names = registry.available_names()
        assert "docker" in names
        assert "monty" in names

    def test_has_provider(self):
        registry = ProviderRegistry()
        registry.register("docker", FakeProvider("docker"))

        assert registry.has("docker") is True
        assert registry.has("e2b") is False

    def test_is_empty_when_no_providers(self):
        registry = ProviderRegistry()
        assert registry.is_empty() is True

    def test_is_not_empty_after_register(self):
        registry = ProviderRegistry()
        registry.register("docker", FakeProvider())
        assert registry.is_empty() is False


class TestAutoSelectProvider:
    """Tests for auto_select (pick best available provider)."""

    def test_prefers_docker_over_e2b(self):
        registry = ProviderRegistry()
        registry.register("docker", FakeProvider("docker"))
        registry.register("e2b", FakeProvider("e2b"))

        assert registry.auto_select() == "docker"

    def test_falls_back_to_e2b(self):
        registry = ProviderRegistry()
        registry.register("e2b", FakeProvider("e2b"))

        assert registry.auto_select() == "e2b"

    def test_monty_excluded_from_auto_select(self):
        """Monty is in-process only; auto_select skips it (Issue #2051)."""
        registry = ProviderRegistry()
        registry.register("monty", FakeProvider("monty"))

        with pytest.raises(ValueError, match="No sandbox providers available"):
            registry.auto_select()

    def test_raises_when_empty(self):
        registry = ProviderRegistry()

        with pytest.raises(ValueError, match="No sandbox providers available"):
            registry.auto_select()


class TestLazyInitialization:
    """Tests for lazy provider initialization."""

    def test_lazy_factory_called_on_first_get(self):
        registry = ProviderRegistry()
        provider = FakeProvider("docker")
        factory_called = {"count": 0}

        def factory() -> SandboxProvider:
            factory_called["count"] += 1
            return provider

        registry.register_lazy("docker", factory)

        # Factory not called yet
        assert factory_called["count"] == 0

        # First get triggers factory
        result = registry.get("docker")
        assert result is provider
        assert factory_called["count"] == 1

        # Second get uses cached instance
        result = registry.get("docker")
        assert result is provider
        assert factory_called["count"] == 1

    def test_lazy_factory_failure_doesnt_register(self):
        registry = ProviderRegistry()

        def failing_factory() -> SandboxProvider:
            raise RuntimeError("Docker not available")

        registry.register_lazy("docker", failing_factory)

        with pytest.raises(RuntimeError, match="Docker not available"):
            registry.get("docker")

        # After failure, factory is retained for retry (not eagerly registered)
        assert registry.has("docker") is True
        assert "docker" not in dict(registry.items())  # Not in eager providers

        # Retry still raises (factory is re-invoked)
        with pytest.raises(RuntimeError, match="Docker not available"):
            registry.get("docker")

    def test_lazy_shows_in_available_names(self):
        registry = ProviderRegistry()
        registry.register_lazy("docker", lambda: FakeProvider())

        assert "docker" in registry.available_names()

    def test_lazy_counts_for_has(self):
        registry = ProviderRegistry()
        registry.register_lazy("docker", lambda: FakeProvider())

        assert registry.has("docker") is True

    def test_lazy_counts_for_is_empty(self):
        registry = ProviderRegistry()
        registry.register_lazy("docker", lambda: FakeProvider())

        assert registry.is_empty() is False
