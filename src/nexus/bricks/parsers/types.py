"""Data types and structures for the parser system."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextChunk:
    """Represents a semantic chunk of text from a document."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    start_index: int = 0
    end_index: int = 0


@dataclass
class ImageData:
    """Represents an extracted image from a document."""

    data: bytes
    format: str  # e.g., "png", "jpg", "gif"
    metadata: dict[str, Any] = field(default_factory=dict)
    width: int | None = None
    height: int | None = None


@dataclass
class ParseResult:
    """Result of parsing a document.

    Attributes:
        text: Extracted plain text content
        metadata: File metadata (MIME type, creation date, etc.)
        structure: Document structure information (headings, sections, etc.)
        chunks: Semantic chunks for embedding/indexing
        images: Extracted images from the document
        raw_content: Optional raw content in original format
    """

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    structure: dict[str, Any] = field(default_factory=dict)
    chunks: list[TextChunk] = field(default_factory=list)
    images: list[ImageData] = field(default_factory=list)
    raw_content: str | None = None

    def __post_init__(self) -> None:
        """Validate and process the parse result."""
        # Ensure text is a string
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")

        # If no chunks provided, create a single chunk from the text
        if not self.chunks and self.text:
            self.chunks = [
                TextChunk(
                    text=self.text,
                    start_index=0,
                    end_index=len(self.text),
                )
            ]
