"""Content service protocol (Issue #1287, Decision 3A).

Defines the contract for content-aware operations that go beyond raw
byte I/O — parsed reads, format conversion, content type detection.

Existing implementation: Scattered across NexusFSCoreMixin.read() kwargs
(parsed=True, format=...) and NexusFS._virtual_view_parse_fn.

This protocol captures the target interface for future extraction.
No implementation required yet (Phase F).

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §3 (Kernel tier)
    - Issue #1287: Extract NexusFS Domain Services from God Object
"""


from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


@runtime_checkable
class ContentServiceProtocol(Protocol):
    """Content-aware file operations — parsing, format conversion.

    Extends basic read/write with content intelligence:
    - Parsed reads (PDF, DOCX, HTML → structured text)
    - Format conversion (Markdown → HTML, etc.)
    - Content type detection
    """

    def read_parsed(
        self,
        path: str,
        *,
        format: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Read file with automatic parsing/conversion.

        Args:
            path: Virtual path to read.
            format: Target format (e.g., "markdown", "text", "html").
                If None, auto-detect based on file type.
            context: Operation context for permission checks.

        Returns:
            Dict with 'content' (parsed text), 'format', 'metadata'.
        """
        ...

    def detect_content_type(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> str:
        """Detect the content type of a file.

        Args:
            path: Virtual path to check.
            context: Operation context for permission checks.

        Returns:
            MIME type string (e.g., "application/pdf", "text/markdown").
        """
        ...
