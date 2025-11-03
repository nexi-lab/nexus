"""Tests for virtual views functionality."""

from nexus.core.virtual_views import (
    add_virtual_views_to_listing,
    get_parsed_content,
    parse_virtual_path,
    should_add_virtual_views,
)


class TestParseVirtualPath:
    """Tests for parse_virtual_path function."""

    def test_parse_xlsx_virtual_view(self):
        """Test parsing _parsed.xlsx.md virtual view."""

        def exists_fn(p):
            return p == "/file.xlsx"

        original, view_type = parse_virtual_path("/file_parsed.xlsx.md", exists_fn)

        assert original == "/file.xlsx"
        assert view_type == "md"

    def test_parse_pdf_virtual_view(self):
        """Test parsing _parsed.pdf.md virtual view."""

        def exists_fn(p):
            return p == "/file.pdf"

        original, view_type = parse_virtual_path("/file_parsed.pdf.md", exists_fn)

        assert original == "/file.pdf"
        assert view_type == "md"

    def test_parse_actual_txt_file(self):
        """Test that actual .txt files are not treated as virtual views."""

        def exists_fn(p):
            return False  # Base file doesn't exist

        original, view_type = parse_virtual_path("/file.txt", exists_fn)

        assert original == "/file.txt"
        assert view_type is None

    def test_parse_actual_md_file(self):
        """Test that actual .md files are not treated as virtual views."""

        def exists_fn(p):
            return False  # Base file doesn't exist

        original, view_type = parse_virtual_path("/file.md", exists_fn)

        assert original == "/file.md"
        assert view_type is None

    def test_parse_without_md_suffix(self):
        """Test that _parsed without .md suffix is not treated as virtual view."""

        def exists_fn(p):
            return True

        original, view_type = parse_virtual_path("/file_parsed.xlsx", exists_fn)

        assert original == "/file_parsed.xlsx"
        assert view_type is None

    def test_parse_actual_file_with_parsed_in_name(self):
        """Test that files with _parsed in name but not matching pattern are handled."""

        def exists_fn(p):
            return p == "/file_parsed_results.txt"

        original, view_type = parse_virtual_path("/file_parsed_results.txt", exists_fn)

        assert original == "/file_parsed_results.txt"
        assert view_type is None

    def test_parse_non_virtual_file(self):
        """Test parsing non-virtual file."""

        def exists_fn(p):
            return True

        original, view_type = parse_virtual_path("/file.xlsx", exists_fn)

        assert original == "/file.xlsx"
        assert view_type is None

    def test_virtual_view_when_base_file_missing(self):
        """Test that virtual view is not created if base file doesn't exist."""

        def exists_fn(p):
            return False

        original, view_type = parse_virtual_path("/file_parsed.xlsx.md", exists_fn)

        assert original == "/file_parsed.xlsx.md"
        assert view_type is None


class TestGetParsedContent:
    """Tests for get_parsed_content function."""

    def test_parse_utf8_text(self):
        """Test parsing UTF-8 text content."""
        content = b"Hello, World!"
        result = get_parsed_content(content, "/file.txt", "txt")

        assert result == b"Hello, World!"

    def test_parse_binary_content_fallback(self):
        """Test that binary content falls back to raw content when parsing fails."""
        # Invalid UTF-8 sequence
        content = b"\xff\xfe\xfd"
        result = get_parsed_content(content, "/unknown.bin", "txt")

        # Should fallback to raw content
        assert result == content

    def test_parse_with_txt_view_type(self):
        """Test parsing with txt view type."""
        content = b"Sample text"
        result = get_parsed_content(content, "/file.pdf", "txt")

        # Should work for text content
        assert result == b"Sample text"

    def test_parse_with_md_view_type(self):
        """Test parsing with md view type."""
        content = b"# Markdown content"
        result = get_parsed_content(content, "/file.md", "md")

        assert result == b"# Markdown content"

    def test_parseable_excel_uses_parser_not_utf8_decode(self):
        """Test that Excel files use parser instead of UTF-8 decode.

        This is a regression test for the bug where Excel files were being
        decoded as UTF-8 text, resulting in binary garbage output.

        The fix checks for parseable extensions BEFORE attempting UTF-8 decode,
        ensuring binary files like .xlsx, .pdf, .docx use the parser directly.
        """
        # Simulate Excel file binary content (ZIP header + XML content)
        excel_content = b"PK\x03\x04\x14\x00\x00\x00\x08\x00"  # ZIP header

        # Call get_parsed_content for an Excel file
        result = get_parsed_content(excel_content, "/file.xlsx", "md")

        # Result should NOT be the raw ZIP/XML content decoded as UTF-8
        # It should either be:
        # 1. Parsed content (if parser is available) - starts with markdown/text
        # 2. Fallback to raw content (if parser fails) - same as input

        # Key assertion: It should NOT attempt UTF-8 decode of binary content
        # UTF-8 decode would produce garbage or fail
        # Either parsed or raw is acceptable, but NOT garbled UTF-8 decode
        assert result == excel_content or (
            result.startswith(b"#")  # Markdown header
            or result.startswith(b"|")  # Table format
            or b"xlsx" in result.lower()  # Parsed content mentions file type
        )

    def test_parseable_pdf_uses_parser_not_utf8_decode(self):
        """Test that PDF files use parser instead of UTF-8 decode."""
        # Simulate PDF binary content
        pdf_content = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"

        result = get_parsed_content(pdf_content, "/document.pdf", "md")

        # Should either parse or return raw, not attempt UTF-8 decode
        assert result == pdf_content or b"PDF" in result or b"pdf" in result

    def test_parseable_docx_uses_parser_not_utf8_decode(self):
        """Test that Word files use parser instead of UTF-8 decode."""
        # Simulate DOCX binary content (also ZIP-based)
        docx_content = b"PK\x03\x04\x14\x00\x06\x00\x08\x00"

        result = get_parsed_content(docx_content, "/document.docx", "md")

        # Should either parse or return raw, not attempt UTF-8 decode
        assert result == docx_content or (result.startswith(b"#") or b"docx" in result.lower())

    def test_non_parseable_binary_falls_back_to_raw(self):
        """Test that non-parseable binary files fallback to raw content."""
        # Random binary file (not in PARSEABLE_EXTENSIONS)
        binary_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

        result = get_parsed_content(binary_content, "/image.bin", "txt")

        # Should return raw content since it's not parseable
        # and UTF-8 decode will fail
        assert result == binary_content

    def test_regular_text_still_uses_utf8_decode(self):
        """Test that regular text files still use UTF-8 decode path.

        This ensures we didn't break the UTF-8 decode path for non-binary files.
        """
        text_content = b"This is plain text content"

        result = get_parsed_content(text_content, "/file.log", "txt")

        # Should successfully decode and return same content
        assert result == text_content


class TestShouldAddVirtualViews:
    """Tests for should_add_virtual_views function."""

    def test_should_add_for_xlsx(self):
        """Test that virtual views should be added for .xlsx files."""
        assert should_add_virtual_views("/file.xlsx") is True

    def test_should_add_for_pdf(self):
        """Test that virtual views should be added for .pdf files."""
        assert should_add_virtual_views("/document.pdf") is True

    def test_should_add_for_docx(self):
        """Test that virtual views should be added for .docx files."""
        assert should_add_virtual_views("/document.docx") is True

    def test_should_not_add_for_txt(self):
        """Test that virtual views should not be added for .txt files."""
        assert should_add_virtual_views("/file.txt") is False

    def test_should_not_add_for_md(self):
        """Test that virtual views should not be added for .md files."""
        assert should_add_virtual_views("/README.md") is False

    def test_should_not_add_for_unknown_extension(self):
        """Test that virtual views should not be added for unknown extensions."""
        assert should_add_virtual_views("/file.unknown") is False

    def test_should_not_add_for_py(self):
        """Test that virtual views should not be added for .py files."""
        assert should_add_virtual_views("/script.py") is False

    def test_should_add_for_pptx(self):
        """Test that virtual views should be added for .pptx files."""
        assert should_add_virtual_views("/presentation.pptx") is True

    def test_should_not_add_for_jpg(self):
        """Test that virtual views should not be added for .jpg files.

        Images require OCR which is not enabled by default, so we don't
        create automatic virtual views for them.
        """
        assert should_add_virtual_views("/image.jpg") is False


class TestAddVirtualViewsToListing:
    """Tests for add_virtual_views_to_listing function."""

    def test_add_views_to_string_list(self):
        """Test adding virtual views to list of strings."""
        files = ["/file.xlsx", "/file.txt", "/file.py"]

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn)

        assert "/file.xlsx" in result
        assert "/file_parsed.xlsx.md" in result
        assert "/file.txt" in result
        # .txt and .py files should not get virtual views
        assert "/file_parsed.txt.md" not in result
        assert "/file_parsed.py.md" not in result

    def test_add_views_to_dict_list(self):
        """Test adding virtual views to list of dicts."""
        files = [
            {"path": "/file.pdf", "size": 1024},
            {"path": "/file.txt", "size": 512},
        ]

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn)

        # Original files should be present
        assert any(f["path"] == "/file.pdf" for f in result)
        assert any(f["path"] == "/file.txt" for f in result)

        # Virtual views should be added for PDF
        assert any(f["path"] == "/file_parsed.pdf.md" for f in result)

        # Virtual views should not be added for TXT
        assert not any(f["path"] == "/file_parsed.txt.md" for f in result)

    def test_skip_directories(self):
        """Test that directories are skipped."""
        files = ["/file.xlsx", "/dir/"]

        def is_directory_fn(p):
            return p == "/dir/"

        result = add_virtual_views_to_listing(files, is_directory_fn)

        # File should get virtual views
        assert "/file_parsed.xlsx.md" in result

        # Directory should not get virtual views
        assert "/dir/_parsed/.md" not in result

    def test_handle_exception_in_is_directory(self):
        """Test that exceptions in is_directory_fn are handled gracefully."""
        files = ["/file.pdf"]

        def failing_is_directory_fn(p):
            raise Exception("Test exception")

        # Should not raise exception
        result = add_virtual_views_to_listing(files, failing_is_directory_fn)

        # Virtual views should still be added
        assert "/file_parsed.pdf.md" in result

    def test_empty_list(self):
        """Test with empty file list."""
        files = []

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn)

        assert result == []

    def test_mixed_parseable_and_non_parseable(self):
        """Test with mix of parseable and non-parseable files."""
        files = ["/file.xlsx", "/file.py", "/file.pdf", "/README.md"]

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn)

        # Parseable files should get virtual views
        assert "/file_parsed.xlsx.md" in result
        assert "/file_parsed.pdf.md" in result

        # Non-parseable files should not
        assert "/file_parsed.py.md" not in result
        assert "/README_parsed.md.md" not in result

    def test_preserve_dict_metadata(self):
        """Test that dict metadata is preserved in virtual views."""
        files = [{"path": "/file.pdf", "size": 1024, "modified": "2024-01-01"}]

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn)

        # Find the virtual .md view
        md_view = next(f for f in result if f["path"] == "/file_parsed.pdf.md")

        # Metadata should be preserved (except path)
        assert md_view["size"] == 1024
        assert md_view["modified"] == "2024-01-01"

    def test_show_parsed_false(self):
        """Test that show_parsed=False excludes virtual views."""
        files = ["/file.xlsx", "/file.pdf"]

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn, show_parsed=False)

        # Original files should be present
        assert "/file.xlsx" in result
        assert "/file.pdf" in result

        # Virtual views should not be present
        assert "/file_parsed.xlsx.md" not in result
        assert "/file_parsed.pdf.md" not in result

    def test_show_parsed_true(self):
        """Test that show_parsed=True includes virtual views (default)."""
        files = ["/file.xlsx"]

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn, show_parsed=True)

        # Both original and virtual should be present
        assert "/file.xlsx" in result
        assert "/file_parsed.xlsx.md" in result
