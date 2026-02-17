"""Tests for BrickContainer DI container (Issue #1393)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pytest

from nexus.core.brick_container import BrickContainer


@runtime_checkable
class FakeProtocol(Protocol):
    """Test protocol for container tests."""

    def do_something(self) -> str: ...


@runtime_checkable
class OtherProtocol(Protocol):
    """Another test protocol."""

    def other_method(self) -> int: ...


class FakeImpl:
    """Implementation satisfying FakeProtocol."""

    def do_something(self) -> str:
        return "done"


class OtherImpl:
    """Implementation satisfying OtherProtocol."""

    def other_method(self) -> int:
        return 42


class BadImpl:
    """Implementation that does NOT satisfy FakeProtocol."""

    pass


class TestBrickContainer:
    """Tests for the BrickContainer class."""

    def test_register_and_resolve(self) -> None:
        container = BrickContainer()
        impl = FakeImpl()
        container.register(FakeProtocol, impl)
        assert container.resolve(FakeProtocol) is impl

    def test_resolve_unregistered_raises_lookup_error(self) -> None:
        container = BrickContainer()
        with pytest.raises(LookupError, match="No implementation registered"):
            container.resolve(FakeProtocol)

    def test_resolve_optional_returns_none_when_missing(self) -> None:
        container = BrickContainer()
        assert container.resolve_optional(FakeProtocol) is None

    def test_resolve_optional_returns_impl_when_present(self) -> None:
        container = BrickContainer()
        impl = FakeImpl()
        container.register(FakeProtocol, impl)
        assert container.resolve_optional(FakeProtocol) is impl

    def test_register_rejects_bad_implementation(self) -> None:
        container = BrickContainer()
        with pytest.raises(TypeError, match="does not satisfy"):
            container.register(FakeProtocol, BadImpl())  # type: ignore[arg-type]

    def test_registered_protocols_empty(self) -> None:
        container = BrickContainer()
        assert container.registered_protocols() == []

    def test_registered_protocols_lists_all(self) -> None:
        container = BrickContainer()
        container.register(FakeProtocol, FakeImpl())
        container.register(OtherProtocol, OtherImpl())
        protocols = container.registered_protocols()
        assert FakeProtocol in protocols
        assert OtherProtocol in protocols
        assert len(protocols) == 2

    def test_invalidate_removes_registration(self) -> None:
        container = BrickContainer()
        container.register(FakeProtocol, FakeImpl())
        container.invalidate(FakeProtocol)
        assert container.resolve_optional(FakeProtocol) is None

    def test_invalidate_nonexistent_is_noop(self) -> None:
        container = BrickContainer()
        container.invalidate(FakeProtocol)  # Should not raise

    def test_register_overwrites_existing(self) -> None:
        container = BrickContainer()
        impl1 = FakeImpl()
        impl2 = FakeImpl()
        container.register(FakeProtocol, impl1)
        container.register(FakeProtocol, impl2)
        assert container.resolve(FakeProtocol) is impl2

    def test_multiple_protocols_independent(self) -> None:
        container = BrickContainer()
        fake = FakeImpl()
        other = OtherImpl()
        container.register(FakeProtocol, fake)
        container.register(OtherProtocol, other)
        assert container.resolve(FakeProtocol) is fake
        assert container.resolve(OtherProtocol) is other
