"""Tests for virtual views functionality."""

from nexus.core.virtual_views import (
    add_virtual_views_to_listing,
    get_parsed_content,
    parse_virtual_path,
    should_add_virtual_views,
)


class TestParseVirtualPath:
    """Tests for parse_virtual_path function."""

    def test_parse_txt_virtual_view(self):
        """Test parsing .txt virtual view."""

        def exists_fn(p):
            return p == "/file.xlsx"

        original, view_type = parse_virtual_path("/file.xlsx.txt", exists_fn)

        assert original == "/file.xlsx"
        assert view_type == "txt"

    def test_parse_md_virtual_view(self):
        """Test parsing .md virtual view."""

        def exists_fn(p):
            return p == "/file.pdf"

        original, view_type = parse_virtual_path("/file.pdf.md", exists_fn)

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

    def test_prevent_double_txt_suffix(self):
        """Test that .txt.txt is not treated as virtual view."""

        def exists_fn(p):
            return True

        original, view_type = parse_virtual_path("/file.txt.txt", exists_fn)

        assert original == "/file.txt.txt"
        assert view_type is None

    def test_prevent_double_md_suffix(self):
        """Test that .md.md is not treated as virtual view."""

        def exists_fn(p):
            return True

        original, view_type = parse_virtual_path("/file.md.md", exists_fn)

        assert original == "/file.md.md"
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

        original, view_type = parse_virtual_path("/file.xlsx.txt", exists_fn)

        assert original == "/file.xlsx.txt"
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

    def test_should_add_for_jpg(self):
        """Test that virtual views should be added for .jpg files."""
        assert should_add_virtual_views("/image.jpg") is True


class TestAddVirtualViewsToListing:
    """Tests for add_virtual_views_to_listing function."""

    def test_add_views_to_string_list(self):
        """Test adding virtual views to list of strings."""
        files = ["/file.xlsx", "/file.txt", "/file.py"]

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn)

        assert "/file.xlsx" in result
        assert "/file.xlsx.txt" in result
        assert "/file.xlsx.md" in result
        assert "/file.txt" in result
        # .txt and .py files should not get virtual views
        assert "/file.txt.txt" not in result
        assert "/file.py.txt" not in result

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
        assert any(f["path"] == "/file.pdf.txt" for f in result)
        assert any(f["path"] == "/file.pdf.md" for f in result)

        # Virtual views should not be added for TXT
        assert not any(f["path"] == "/file.txt.txt" for f in result)

    def test_skip_directories(self):
        """Test that directories are skipped."""
        files = ["/file.xlsx", "/dir/"]

        def is_directory_fn(p):
            return p == "/dir/"

        result = add_virtual_views_to_listing(files, is_directory_fn)

        # File should get virtual views
        assert "/file.xlsx.txt" in result
        assert "/file.xlsx.md" in result

        # Directory should not get virtual views
        assert "/dir/.txt" not in result
        assert "/dir/.md" not in result

    def test_handle_exception_in_is_directory(self):
        """Test that exceptions in is_directory_fn are handled gracefully."""
        files = ["/file.pdf"]

        def failing_is_directory_fn(p):
            raise Exception("Test exception")

        # Should not raise exception
        result = add_virtual_views_to_listing(files, failing_is_directory_fn)

        # Virtual views should still be added
        assert "/file.pdf.txt" in result
        assert "/file.pdf.md" in result

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
        assert "/file.xlsx.txt" in result
        assert "/file.xlsx.md" in result
        assert "/file.pdf.txt" in result
        assert "/file.pdf.md" in result

        # Non-parseable files should not
        assert "/file.py.txt" not in result
        assert "/README.md.txt" not in result

    def test_preserve_dict_metadata(self):
        """Test that dict metadata is preserved in virtual views."""
        files = [{"path": "/file.pdf", "size": 1024, "modified": "2024-01-01"}]

        def is_directory_fn(p):
            return False

        result = add_virtual_views_to_listing(files, is_directory_fn)

        # Find the virtual .txt view
        txt_view = next(f for f in result if f["path"] == "/file.pdf.txt")

        # Metadata should be preserved (except path)
        assert txt_view["size"] == 1024
        assert txt_view["modified"] == "2024-01-01"
