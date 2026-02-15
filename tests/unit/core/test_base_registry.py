"""Unit tests for BaseRegistry[T]."""

import pytest

from nexus.core.registry import BaseRegistry


class TestRegisterAndGet:
    """register / get / get_or_raise."""

    def test_register_and_get(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        reg.register("a", 1)
        assert reg.get("a") == 1

    def test_get_missing_returns_none(self) -> None:
        reg: BaseRegistry[str] = BaseRegistry("s")
        assert reg.get("nope") is None

    def test_get_or_raise_missing(self) -> None:
        reg: BaseRegistry[str] = BaseRegistry("s")
        reg.register("x", "val")
        with pytest.raises(KeyError, match="not found"):
            reg.get_or_raise("y")

    def test_get_or_raise_shows_available(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        reg.register("alpha", 1)
        reg.register("beta", 2)
        with pytest.raises(KeyError, match="alpha") as exc_info:
            reg.get_or_raise("gamma")
        assert "beta" in str(exc_info.value)

    def test_duplicate_raises(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        reg.register("a", 1)
        with pytest.raises(ValueError, match="already registered"):
            reg.register("a", 2)

    def test_allow_overwrite(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        reg.register("a", 1)
        reg.register("a", 99, allow_overwrite=True)
        assert reg.get("a") == 99


class TestUnregister:

    def test_unregister_returns_item(self) -> None:
        reg: BaseRegistry[str] = BaseRegistry("s")
        reg.register("k", "v")
        assert reg.unregister("k") == "v"
        assert reg.get("k") is None

    def test_unregister_missing_returns_none(self) -> None:
        reg: BaseRegistry[str] = BaseRegistry("s")
        assert reg.unregister("nope") is None


class TestListAndIteration:

    def test_list_names_sorted(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        reg.register("c", 3)
        reg.register("a", 1)
        reg.register("b", 2)
        assert reg.list_names() == ["a", "b", "c"]

    def test_list_all_sorted_by_key(self) -> None:
        reg: BaseRegistry[str] = BaseRegistry("s")
        reg.register("z", "last")
        reg.register("a", "first")
        assert reg.list_all() == ["first", "last"]

    def test_iter_sorted(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        reg.register("b", 2)
        reg.register("a", 1)
        assert list(reg) == ["a", "b"]


class TestClearLenContains:

    def test_clear(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        reg.register("a", 1)
        reg.register("b", 2)
        reg.clear()
        assert len(reg) == 0
        assert reg.list_names() == []

    def test_len(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        assert len(reg) == 0
        reg.register("a", 1)
        assert len(reg) == 1

    def test_contains(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("nums")
        reg.register("a", 1)
        assert "a" in reg
        assert "b" not in reg


class TestRepr:

    def test_repr(self) -> None:
        reg: BaseRegistry[int] = BaseRegistry("my_reg")
        reg.register("x", 1)
        r = repr(reg)
        assert "BaseRegistry" in r
        assert "my_reg" in r
        assert "x" in r


class TestProtocolValidation:

    def test_register_with_protocol_passes(self) -> None:
        from typing import Protocol, runtime_checkable

        @runtime_checkable
        class HasName(Protocol):
            @property
            def name(self) -> str: ...

        class Good:
            @property
            def name(self) -> str:
                return "good"

        reg: BaseRegistry[Good] = BaseRegistry("p", protocol=HasName)
        reg.register("g", Good())  # should not raise

    def test_register_with_protocol_fails(self) -> None:
        from typing import Protocol, runtime_checkable

        @runtime_checkable
        class HasName(Protocol):
            @property
            def name(self) -> str: ...

        class Bad:
            pass

        reg: BaseRegistry[Bad] = BaseRegistry("p", protocol=HasName)
        with pytest.raises(TypeError, match="does not satisfy"):
            reg.register("b", Bad())


class TestDiscoverFromPackage:

    def test_discover_parsers(self) -> None:
        from nexus.parsers.base import Parser

        reg: BaseRegistry[Parser] = BaseRegistry("test_parsers")
        count = reg.discover_from_package(
            "nexus.parsers",
            Parser,
            key_fn=lambda cls: cls.__name__,
        )
        assert count >= 1  # at least MarkItDownParser

    def test_discover_nonexistent_package(self) -> None:
        reg: BaseRegistry[object] = BaseRegistry("empty")
        count = reg.discover_from_package("nonexistent.pkg", object)
        assert count == 0
