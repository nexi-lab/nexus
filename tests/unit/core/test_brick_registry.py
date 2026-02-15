"""Unit tests for BrickRegistry."""

from typing import Protocol, runtime_checkable

import pytest

from nexus.core.registry import BrickInfo, BrickRegistry


@runtime_checkable
class Greetable(Protocol):
    def greet(self) -> str: ...


class HelloBrick:
    def greet(self) -> str:
        return "hello"


class BadBrick:
    pass


@pytest.fixture()
def registry() -> BrickRegistry:
    return BrickRegistry()


class TestRegisterBrick:
    def test_register_compliant(self, registry: BrickRegistry) -> None:
        registry.register_brick("hello", HelloBrick, Greetable)
        info = registry.get_or_raise("hello")
        assert isinstance(info, BrickInfo)
        assert info.brick_cls is HelloBrick
        assert info.protocol is Greetable

    def test_register_non_compliant_raises(self, registry: BrickRegistry) -> None:
        with pytest.raises(TypeError, match="does not satisfy"):
            registry.register_brick("bad", BadBrick, Greetable)

    def test_register_with_metadata(self, registry: BrickRegistry) -> None:
        registry.register_brick("hello", HelloBrick, Greetable, metadata={"v": "1.0"})
        info = registry.get_or_raise("hello")
        assert info.metadata == {"v": "1.0"}

    def test_duplicate_raises_without_overwrite(self, registry: BrickRegistry) -> None:
        registry.register_brick("hello", HelloBrick, Greetable)
        with pytest.raises(ValueError, match="already registered"):
            registry.register_brick("hello", HelloBrick, Greetable)

    def test_allow_overwrite(self, registry: BrickRegistry) -> None:
        registry.register_brick("hello", HelloBrick, Greetable, metadata={"v": "1"})
        registry.register_brick(
            "hello", HelloBrick, Greetable, metadata={"v": "2"}, allow_overwrite=True
        )
        assert registry.get_or_raise("hello").metadata == {"v": "2"}


class TestListByProtocol:
    def test_filter(self, registry: BrickRegistry) -> None:
        @runtime_checkable
        class Other(Protocol):
            def other(self) -> int: ...

        class OtherBrick:
            def other(self) -> int:
                return 42

        registry.register_brick("h", HelloBrick, Greetable)
        registry.register_brick("o", OtherBrick, Other)

        greetables = registry.list_by_protocol(Greetable)
        assert len(greetables) == 1
        assert greetables[0].name == "h"


class TestGetBrickClass:
    def test_get_brick_class(self, registry: BrickRegistry) -> None:
        registry.register_brick("hello", HelloBrick, Greetable)
        assert registry.get_brick_class("hello") is HelloBrick

    def test_get_brick_class_missing(self, registry: BrickRegistry) -> None:
        with pytest.raises(KeyError):
            registry.get_brick_class("nope")


class TestBrickInfoFrozen:
    def test_frozen_attribute(self) -> None:
        info = BrickInfo(name="x", brick_cls=HelloBrick, protocol=Greetable)
        with pytest.raises(AttributeError):
            info.name = "y"  # type: ignore[misc]

    def test_metadata_immutable(self, registry: BrickRegistry) -> None:
        registry.register_brick("hello", HelloBrick, Greetable, metadata={"v": "1"})
        info = registry.get_or_raise("hello")
        with pytest.raises(TypeError):
            info.metadata["v"] = "2"  # type: ignore[index]
