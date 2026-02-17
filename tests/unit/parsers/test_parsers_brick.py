"""Tests for ParsersBrick facade — single entry point for parsing (Issue #1523).

These tests will initially FAIL (RED) until ParsersBrick is implemented in Phase 4.
"""

from __future__ import annotations


class TestParsersBrickConstruction:
    def test_construct_default(self) -> None:
        from nexus.parsers.brick import ParsersBrick

        brick = ParsersBrick()
        assert brick.parser_registry is not None
        assert brick.provider_registry is not None

    def test_parser_registry_has_markitdown(self) -> None:
        from nexus.parsers.brick import ParsersBrick

        brick = ParsersBrick()
        parsers = brick.parser_registry.get_parsers()
        names = [p.name for p in parsers]
        assert "MarkItDownParser" in names


class TestCreateParseFn:
    def test_create_parse_fn_returns_callable(self) -> None:
        from nexus.parsers.brick import ParsersBrick

        brick = ParsersBrick()
        fn = brick.create_parse_fn()
        assert callable(fn)


class TestSharedRegistry:
    def test_registries_are_stable_references(self) -> None:
        from nexus.parsers.brick import ParsersBrick

        brick = ParsersBrick()
        r1 = brick.parser_registry
        r2 = brick.parser_registry
        assert r1 is r2

    def test_provider_registry_stable(self) -> None:
        from nexus.parsers.brick import ParsersBrick

        brick = ParsersBrick()
        r1 = brick.provider_registry
        r2 = brick.provider_registry
        assert r1 is r2
