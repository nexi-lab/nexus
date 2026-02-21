"""Tests for detection utilities — MIME type, encoding, compression (Issue #1523)."""

import gzip

import pytest

from nexus.parsers.detection import (
    decompress_content,
    detect_encoding,
    detect_mime_type,
    is_compressed,
    prepare_content_for_parsing,
)


class TestDetectMimeType:
    def test_returns_none_for_unknown(self) -> None:
        result = detect_mime_type(b"\x00\x01\x02\x03")
        # May return None or a generic type depending on magic availability
        assert result is None or isinstance(result, str)

    def test_extension_fallback(self) -> None:
        result = detect_mime_type(b"hello", "test.txt")
        assert result is not None
        assert "text" in result


class TestDetectEncoding:
    def test_utf8_default(self) -> None:
        result = detect_encoding(b"hello world")
        assert isinstance(result, str)
        # Should return some encoding (utf-8 or ascii)
        assert result.lower() in ("utf-8", "ascii", "us-ascii")

    def test_returns_string(self) -> None:
        result = detect_encoding(b"\xff\xfe\x00\x00")
        assert isinstance(result, str)


class TestIsCompressed:
    def test_gz_compressed(self) -> None:
        assert is_compressed("file.gz") is True

    def test_zip_compressed(self) -> None:
        assert is_compressed("file.zip") is True

    def test_bz2_compressed(self) -> None:
        assert is_compressed("file.bz2") is True

    def test_xz_compressed(self) -> None:
        assert is_compressed("file.xz") is True

    def test_not_compressed(self) -> None:
        assert is_compressed("file.txt") is False
        assert is_compressed("file.pdf") is False


class TestDecompressContent:
    def test_decompress_gzip(self) -> None:
        original = b"hello gzipped world"
        compressed = gzip.compress(original)
        result, inner_name = decompress_content(compressed, "data.txt.gz")
        assert result == original
        assert inner_name == "data.txt"

    def test_decompress_not_compressed_passthrough(self) -> None:
        content = b"plain text"
        result, inner_name = decompress_content(content, "file.txt")
        assert result == content
        assert inner_name is None

    def test_invalid_gzip_raises(self) -> None:
        with pytest.raises(ValueError, match="Failed to decompress"):
            decompress_content(b"not gzip", "file.gz")

    def test_decompress_bz2(self) -> None:
        import bz2

        original = b"hello bz2 world"
        compressed = bz2.compress(original)
        result, inner_name = decompress_content(compressed, "data.txt.bz2")
        assert result == original
        assert inner_name == "data.txt"

    def test_decompress_xz(self) -> None:
        import lzma

        original = b"hello xz world"
        compressed = lzma.compress(original)
        result, inner_name = decompress_content(compressed, "data.txt.xz")
        assert result == original
        assert inner_name == "data.txt"


class TestPrepareContentForParsing:
    def test_plain_file(self) -> None:
        content = b"plain text"
        processed, path, metadata = prepare_content_for_parsing(content, "file.txt")
        assert processed == content
        assert path == "file.txt"

    def test_compressed_file(self) -> None:
        original = b"decompressed content"
        compressed = gzip.compress(original)
        processed, path, metadata = prepare_content_for_parsing(compressed, "data.txt.gz")
        assert processed == original
        assert metadata.get("compressed") is True
        assert path == "data.txt"
