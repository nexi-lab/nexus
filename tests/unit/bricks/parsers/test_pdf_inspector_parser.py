"""Tests for PdfInspectorParser (brick-level adapter)."""

import pathlib
import sys

import pytest

from nexus.bricks.parsers.pdf_inspector_parser import PdfInspectorParser
from nexus.contracts.exceptions import ParserError

FIXTURES = pathlib.Path(__file__).parent / "providers" / "fixtures"


def test_supported_formats_is_pdf_only():
    parser = PdfInspectorParser()
    assert parser.supported_formats == [".pdf"]


def test_priority_outranks_default():
    parser = PdfInspectorParser()
    # Parser base class returns default priority 0
    assert parser.priority > 0


def test_can_parse_pdf_when_available():
    pytest.importorskip("pdf_inspector")
    parser = PdfInspectorParser()
    assert parser.can_parse("file.pdf") is True


def test_can_parse_rejects_non_pdf():
    pytest.importorskip("pdf_inspector")
    parser = PdfInspectorParser()
    assert parser.can_parse("file.docx") is False
    assert parser.can_parse("file.txt") is False


def test_can_parse_returns_false_when_pdf_inspector_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)
    parser = PdfInspectorParser()
    assert parser.can_parse("file.pdf") is False


@pytest.mark.asyncio
async def test_parse_delegates_to_provider():
    pytest.importorskip("pdf_inspector")
    parser = PdfInspectorParser()
    content = (FIXTURES / "hello_text.pdf").read_bytes()

    result = await parser.parse(content, {"path": "hello.pdf"})

    assert "Hello World" in result.text
    assert result.metadata["parser"] == "pdf-inspector"
    assert result.metadata["format"] == ".pdf"
    assert result.metadata["pdf_type"] == "text_based"


@pytest.mark.asyncio
async def test_parse_uses_filename_fallback():
    pytest.importorskip("pdf_inspector")
    parser = PdfInspectorParser()
    content = (FIXTURES / "hello_text.pdf").read_bytes()

    result = await parser.parse(content, {"filename": "x.pdf"})

    assert result.metadata["original_path"] == "x.pdf"


@pytest.mark.asyncio
async def test_parse_invalid_bytes_raises_parser_error():
    pytest.importorskip("pdf_inspector")
    parser = PdfInspectorParser()

    with pytest.raises(ParserError) as exc_info:
        await parser.parse(b"not a real pdf", {"path": "broken.pdf"})

    assert exc_info.value.parser == "pdf-inspector"


# Regression: ensure ParsersBrick's create_parse_fn routes PDFs to
# pdf-inspector on a default install.  Confirms the brick wires pdf-inspector
# into the auto-parse-on-write pipeline (``create_parse_fn`` /
# ``AutoParseWriteHook``) so PDFs flow to the indexer.


def test_brick_create_parse_fn_handles_pdf():
    """Regression test for auto-parse-on-write path (issue #3757).

    ``ParsersBrick.create_parse_fn`` resolves parsers via ``ParserRegistry``
    (not ``ProviderRegistry``). Confirms that PdfInspectorParser is wired
    into the auto-parse pipeline so PDFs are parsed on write.
    """
    pytest.importorskip("pdf_inspector")
    from nexus.bricks.parsers.brick import ParsersBrick

    brick = ParsersBrick()
    parse = brick.create_parse_fn()
    content = (FIXTURES / "hello_text.pdf").read_bytes()

    result = parse(content, "hello.pdf")

    assert result is not None
    assert b"Hello World" in result
