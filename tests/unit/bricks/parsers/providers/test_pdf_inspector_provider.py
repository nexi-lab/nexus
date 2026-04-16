"""Tests for PdfInspectorProvider."""

import pathlib
import sys

import pytest

from nexus.bricks.parsers.providers.pdf_inspector_provider import PdfInspectorProvider

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_default_formats_is_pdf_only():
    provider = PdfInspectorProvider()
    assert provider.default_formats == [".pdf"]


def test_name():
    provider = PdfInspectorProvider()
    assert provider.name == "pdf-inspector"


def test_is_available_when_installed():
    provider = PdfInspectorProvider()
    pytest.importorskip("pdf_inspector")
    assert provider.is_available() is True


def test_is_available_returns_false_when_import_fails(monkeypatch):
    provider = PdfInspectorProvider()
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)
    assert provider.is_available() is False
