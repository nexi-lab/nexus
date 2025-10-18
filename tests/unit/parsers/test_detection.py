"""Tests for document detection utilities."""

import gzip
import io
import zipfile

import pytest

from nexus.parsers.detection import (
    decompress_content,
    detect_encoding,
    detect_mime_type,
    is_compressed,
    prepare_content_for_parsing,
)


class TestMimeTypeDetection:
    """Tests for MIME type detection."""

    def test_detect_mime_text(self):
        """Test MIME type detection for text files."""
        content = b"Hello, world!"
        mime_type = detect_mime_type(content, "test.txt")
        assert mime_type is not None
        assert "text" in mime_type.lower()

    def test_detect_mime_json(self):
        """Test MIME type detection for JSON files."""
        content = b'{"key": "value"}'
        mime_type = detect_mime_type(content, "test.json")
        assert mime_type is not None
        # Could be application/json or text/plain depending on detection
        assert mime_type in ["application/json", "text/plain"]

    def test_detect_mime_extension_fallback(self):
        """Test MIME type detection falls back to extension."""
        content = b"some content"
        mime_type = detect_mime_type(content, "test.py")
        # Extension-based fallback should work
        assert mime_type is not None

    def test_detect_mime_no_path(self):
        """Test MIME type detection without file path."""
        content = b"Hello, world!"
        mime_type = detect_mime_type(content)
        # Should still work with magic, or return None
        assert mime_type is None or isinstance(mime_type, str)


class TestEncodingDetection:
    """Tests for encoding detection."""

    def test_detect_utf8(self):
        """Test UTF-8 encoding detection."""
        content = "Hello, 世界! 🌍".encode()
        encoding = detect_encoding(content)
        assert encoding.lower() in ["utf-8", "utf8", "ascii"]

    def test_detect_ascii(self):
        """Test ASCII encoding detection."""
        content = b"Hello, world!"
        encoding = detect_encoding(content)
        assert encoding.lower() in ["ascii", "utf-8", "utf8"]

    def test_detect_latin1(self):
        """Test Latin-1 encoding detection."""
        content = "Héllo, wörld!".encode("latin-1")
        encoding = detect_encoding(content)
        # Should detect some encoding
        assert isinstance(encoding, str)

    def test_detect_empty_content(self):
        """Test encoding detection with empty content."""
        content = b""
        encoding = detect_encoding(content)
        # Should return default encoding
        assert encoding == "utf-8"


class TestCompressionDetection:
    """Tests for compression detection."""

    def test_is_compressed_gz(self):
        """Test gzip compression detection."""
        assert is_compressed("file.gz")
        assert is_compressed("file.gzip")

    def test_is_compressed_zip(self):
        """Test ZIP compression detection."""
        assert is_compressed("file.zip")

    def test_is_compressed_bz2(self):
        """Test bz2 compression detection."""
        assert is_compressed("file.bz2")

    def test_is_compressed_xz(self):
        """Test xz compression detection."""
        assert is_compressed("file.xz")

    def test_is_not_compressed(self):
        """Test uncompressed file detection."""
        assert not is_compressed("file.txt")
        assert not is_compressed("file.pdf")
        assert not is_compressed("file.json")


class TestDecompression:
    """Tests for file decompression."""

    def test_decompress_gzip(self):
        """Test gzip decompression."""
        original = b"Hello, world! This is test content."
        compressed = gzip.compress(original)

        decompressed, inner_name = decompress_content(compressed, "test.txt.gz")

        assert decompressed == original
        assert inner_name == "test.txt"

    def test_decompress_zip_single_file(self):
        """Test ZIP decompression with single file."""
        original = b"Hello, world! This is test content."

        # Create ZIP file in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("test.txt", original)
        compressed = zip_buffer.getvalue()

        decompressed, inner_name = decompress_content(compressed, "archive.zip")

        assert decompressed == original
        assert inner_name == "test.txt"

    def test_decompress_zip_multiple_files(self):
        """Test ZIP decompression with multiple files fails."""
        # Create ZIP file with multiple files
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("file1.txt", b"content1")
            zf.writestr("file2.txt", b"content2")
        compressed = zip_buffer.getvalue()

        with pytest.raises(ValueError, match="Multi-file archives"):
            decompress_content(compressed, "archive.zip")

    def test_decompress_not_compressed(self):
        """Test decompression with uncompressed file."""
        content = b"Hello, world!"
        result, inner_name = decompress_content(content, "test.txt")

        assert result == content
        assert inner_name is None

    def test_decompress_invalid_gzip(self):
        """Test decompression with invalid gzip content."""
        invalid_content = b"This is not gzip content"

        with pytest.raises(ValueError, match="Failed to decompress gzip"):
            decompress_content(invalid_content, "test.gz")

    def test_decompress_bz2(self):
        """Test bz2 decompression."""
        import bz2

        original = b"Hello, world! This is test content."
        compressed = bz2.compress(original)

        decompressed, inner_name = decompress_content(compressed, "test.txt.bz2")

        assert decompressed == original
        assert inner_name == "test.txt"

    def test_decompress_xz(self):
        """Test xz/lzma decompression."""
        import lzma

        original = b"Hello, world! This is test content."
        compressed = lzma.compress(original)

        decompressed, inner_name = decompress_content(compressed, "test.txt.xz")

        assert decompressed == original
        assert inner_name == "test.txt"


class TestPrepareContentForParsing:
    """Tests for prepare_content_for_parsing utility."""

    def test_prepare_plain_content(self):
        """Test preparation with plain content."""
        content = b"Hello, world!"
        file_path = "test.txt"

        processed, effective_path, metadata = prepare_content_for_parsing(content, file_path)

        assert processed == content
        assert effective_path == file_path
        assert "mime_type" in metadata

    def test_prepare_compressed_content(self):
        """Test preparation with compressed content."""
        original = b"Hello, world!"
        compressed = gzip.compress(original)
        file_path = "test.txt.gz"

        processed, effective_path, metadata = prepare_content_for_parsing(compressed, file_path)

        assert processed == original
        assert effective_path == "test.txt"
        assert metadata.get("compressed") is True
        assert metadata.get("inner_filename") == "test.txt"
        assert "mime_type" in metadata

    def test_prepare_json_content(self):
        """Test preparation with JSON content."""
        content = b'{"key": "value"}'
        file_path = "test.json"

        processed, effective_path, metadata = prepare_content_for_parsing(content, file_path)

        assert processed == content
        assert effective_path == file_path
        assert "mime_type" in metadata

    def test_prepare_with_compression_error(self):
        """Test preparation with compression error."""
        invalid_gzip = b"not real gzip content"
        file_path = "test.txt.gz"

        processed, effective_path, metadata = prepare_content_for_parsing(invalid_gzip, file_path)

        # Should handle error gracefully
        assert "compression_error" in metadata
        assert processed == invalid_gzip  # Returns original on error
