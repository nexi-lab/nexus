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


@pytest.mark.asyncio
async def test_parse_text_pdf_returns_markdown_and_metadata():
    pytest.importorskip("pdf_inspector")
    provider = PdfInspectorProvider()
    content = (FIXTURES / "hello_text.pdf").read_bytes()

    result = await provider.parse(content, "hello_text.pdf")

    assert "Hello World" in result.text
    assert result.metadata["parser"] == "pdf-inspector"
    assert result.metadata["format"] == ".pdf"
    assert result.metadata["original_path"] == "hello_text.pdf"
    assert result.metadata["pdf_type"] == "text_based"
    assert result.metadata["pages_needing_ocr"] == []
    assert result.metadata["requires_ocr"] is False
    assert result.metadata["has_encoding_issues"] is False
    assert result.chunks  # non-empty
    assert isinstance(result.structure, dict)
