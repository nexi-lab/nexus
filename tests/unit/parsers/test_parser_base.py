"""Unit tests for Parser base class."""

import pytest

from nexus.parsers.base import Parser
from nexus.parsers.types import ParseResult


class ConcreteParser(Parser):
    """Concrete implementation of Parser for testing."""

    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        """Check if this parser can handle the given file."""
        return file_path.endswith(".test")

    async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
        """Parse file content."""
        text = content.decode("utf-8")
        return ParseResult(text=text, metadata=metadata or {})

    @property
    def supported_formats(self) -> list[str]:
        """List of supported file extensions."""
        return [".test"]


class CustomNameParser(ConcreteParser):
    """Parser with custom name override."""

    @property
    def name(self) -> str:
        """Custom parser name."""
        return "CustomTestParser"


class CustomPriorityParser(ConcreteParser):
    """Parser with custom priority."""

    @property
    def priority(self) -> int:
        """Custom parser priority."""
        return 10


class TestParserName:
    """Test Parser name property."""

    def test_name_default_returns_class_name(self) -> None:
        """Test that default name returns the class name."""
        parser = ConcreteParser()
        assert parser.name == "ConcreteParser"

    def test_name_can_be_overridden(self) -> None:
        """Test that name can be overridden in subclass."""
        parser = CustomNameParser()
        assert parser.name == "CustomTestParser"


class TestParserPriority:
    """Test Parser priority property."""

    def test_priority_default_is_zero(self) -> None:
        """Test that default priority is 0."""
        parser = ConcreteParser()
        assert parser.priority == 0

    def test_priority_can_be_overridden(self) -> None:
        """Test that priority can be overridden in subclass."""
        parser = CustomPriorityParser()
        assert parser.priority == 10


class TestParserGetFileExtension:
    """Test Parser _get_file_extension helper method."""

    def test_get_file_extension_with_dot(self) -> None:
        """Test extracting extension with dot."""
        parser = ConcreteParser()
        assert parser._get_file_extension("file.txt") == ".txt"
        assert parser._get_file_extension("document.pdf") == ".pdf"
        assert parser._get_file_extension("archive.tar.gz") == ".gz"

    def test_get_file_extension_uppercase(self) -> None:
        """Test that extension is lowercased."""
        parser = ConcreteParser()
        assert parser._get_file_extension("FILE.TXT") == ".txt"
        assert parser._get_file_extension("Document.PDF") == ".pdf"
        assert parser._get_file_extension("Image.PNG") == ".png"

    def test_get_file_extension_with_path(self) -> None:
        """Test extracting extension from full path."""
        parser = ConcreteParser()
        assert parser._get_file_extension("/path/to/file.txt") == ".txt"
        assert parser._get_file_extension("C:\\Users\\file.docx") == ".docx"
        assert parser._get_file_extension("./relative/path/file.md") == ".md"

    def test_get_file_extension_no_extension(self) -> None:
        """Test file with no extension."""
        parser = ConcreteParser()
        assert parser._get_file_extension("README") == ""
        assert parser._get_file_extension("/path/to/Makefile") == ""


class TestParserAbstractMethods:
    """Test that abstract methods must be implemented."""

    def test_cannot_instantiate_parser_directly(self) -> None:
        """Test that Parser cannot be instantiated directly."""
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            Parser()  # type: ignore[abstract]

    def test_can_parse_must_be_implemented(self) -> None:
        """Test that can_parse must be implemented in subclass."""

        class IncompleteParser(Parser):
            """Parser missing can_parse implementation."""

            async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
                return ParseResult(text="")

            @property
            def supported_formats(self) -> list[str]:
                return [".test"]

        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteParser()  # type: ignore[abstract]

    def test_parse_must_be_implemented(self) -> None:
        """Test that parse must be implemented in subclass."""

        class IncompleteParser(Parser):
            """Parser missing parse implementation."""

            def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
                return True

            @property
            def supported_formats(self) -> list[str]:
                return [".test"]

        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteParser()  # type: ignore[abstract]

    def test_supported_formats_must_be_implemented(self) -> None:
        """Test that supported_formats must be implemented in subclass."""

        class IncompleteParser(Parser):
            """Parser missing supported_formats implementation."""

            def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
                return True

            async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
                return ParseResult(text="")

        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteParser()  # type: ignore[abstract]


class TestConcreteParserImplementation:
    """Test concrete parser implementation."""

    def test_can_parse_returns_correct_value(self) -> None:
        """Test that can_parse works correctly."""
        parser = ConcreteParser()
        assert parser.can_parse("file.test") is True
        assert parser.can_parse("file.txt") is False

    async def test_parse_returns_parse_result(self) -> None:
        """Test that parse returns a ParseResult."""
        parser = ConcreteParser()
        content = b"test content"
        result = await parser.parse(content)

        assert isinstance(result, ParseResult)
        assert result.text == "test content"

    async def test_parse_with_metadata(self) -> None:
        """Test parsing with metadata."""
        parser = ConcreteParser()
        content = b"test content"
        metadata = {"file_path": "/path/to/file.test"}

        result = await parser.parse(content, metadata=metadata)

        assert result.text == "test content"
        assert result.metadata == {"file_path": "/path/to/file.test"}

    def test_supported_formats_returns_list(self) -> None:
        """Test that supported_formats returns a list."""
        parser = ConcreteParser()
        formats = parser.supported_formats

        assert isinstance(formats, list)
        assert ".test" in formats
