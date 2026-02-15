"""Hypothesis property-based tests for validation parsers.

Ensures parsers never crash on arbitrary input — they should always
return a list (possibly empty) without raising exceptions.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.validation.parsers.clippy import CargoClippyValidator
from nexus.validation.parsers.eslint import ESLintValidator
from nexus.validation.parsers.mypy import MypyValidator
from nexus.validation.parsers.ruff import RuffValidator
from nexus.validation.models import ValidationError


class TestRuffParserFuzz:
    @given(stdout=st.text(max_size=2000))
    @settings(max_examples=50)
    def test_never_crashes(self, stdout: str):
        parser = RuffValidator()
        result = parser.parse_output(stdout, "", 1)
        assert isinstance(result, list)
        for err in result:
            assert isinstance(err, ValidationError)


class TestMypyParserFuzz:
    @given(stdout=st.text(max_size=2000))
    @settings(max_examples=50)
    def test_never_crashes(self, stdout: str):
        parser = MypyValidator()
        result = parser.parse_output(stdout, "", 1)
        assert isinstance(result, list)
        for err in result:
            assert isinstance(err, ValidationError)


class TestESLintParserFuzz:
    @given(stdout=st.text(max_size=2000))
    @settings(max_examples=50)
    def test_never_crashes(self, stdout: str):
        parser = ESLintValidator()
        result = parser.parse_output(stdout, "", 1)
        assert isinstance(result, list)
        for err in result:
            assert isinstance(err, ValidationError)


class TestClippyParserFuzz:
    @given(stdout=st.text(max_size=2000))
    @settings(max_examples=50)
    def test_never_crashes(self, stdout: str):
        parser = CargoClippyValidator()
        result = parser.parse_output(stdout, "", 1)
        assert isinstance(result, list)
        for err in result:
            assert isinstance(err, ValidationError)


class TestValidationErrorBoundary:
    @given(
        line=st.integers(min_value=-1000, max_value=1_000_000),
        column=st.integers(min_value=-1000, max_value=1_000_000),
        message=st.text(max_size=500),
    )
    @settings(max_examples=30)
    def test_model_accepts_boundary_values(self, line: int, column: int, message: str):
        """ValidationError model handles extreme values gracefully."""
        err = ValidationError(
            file="test.py",
            line=line,
            column=column,
            severity="error",
            message=message,
        )
        assert err.line == line
        assert err.column == column
