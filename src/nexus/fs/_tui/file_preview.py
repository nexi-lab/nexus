"""File preview widget — syntax-highlighted text, hex for binary, empty file handling."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from nexus.fs._tui.auth_guidance import format_runtime_error

MAX_PREVIEW_BYTES = 1_048_576  # 1 MB

# File extensions known to be binary
_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".o",
        ".a",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".flac",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".pyc",
        ".pyo",
        ".class",
        ".wasm",
        ".db",
        ".sqlite",
        ".sqlite3",
    }
)

# Extension → Pygments lexer name
_EXTENSION_LEXERS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".md": "markdown",
    ".txt": "text",
    ".csv": "text",
    ".ini": "ini",
    ".cfg": "ini",
    ".dockerfile": "docker",
    ".tf": "terraform",
    ".proto": "protobuf",
    ".r": "r",
    ".R": "r",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".lua": "lua",
}


def _guess_lexer(path: str) -> str:
    """Guess the Pygments lexer name from a file path."""
    suffix = PurePosixPath(path).suffix.lower()
    name = PurePosixPath(path).name.lower()

    # Special filenames
    if name == "dockerfile":
        return "docker"
    if name == "makefile":
        return "makefile"
    if name in ("cmakelists.txt",):
        return "cmake"

    return _EXTENSION_LEXERS.get(suffix, "text")


def _is_likely_binary(content: bytes) -> bool:
    """Heuristic check for binary content (null bytes in first 8KB)."""
    sample = content[:8192]
    return b"\x00" in sample


def _hex_preview(content: bytes, max_lines: int = 32) -> str:
    """Generate a hex dump preview."""
    lines = []
    for offset in range(0, min(len(content), max_lines * 16), 16):
        chunk = content[offset : offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_part:<48}  {ascii_part}")
    return "\n".join(lines)


class FilePreview(Widget):
    """File preview with syntax highlighting, binary hex view, and empty file handling.

    Uses read_range() to fetch at most 1MB, avoiding full download of large files.

    Attributes:
        file_path: Currently previewed file path.
    """

    DEFAULT_CSS = """
    FilePreview {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        padding: 1;
    }
    FilePreview .preview-header {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }
    FilePreview .preview-content {
        padding: 0;
    }
    FilePreview .preview-empty {
        text-align: center;
        padding: 4 2;
        color: $text-muted;
    }
    """

    file_path: reactive[str] = reactive("")

    def __init__(self, fs: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fs = fs

    def compose(self) -> ComposeResult:
        yield Static("", id="preview-header", classes="preview-header")
        yield Static(
            "[dim]Select a file to preview[/dim]",
            id="preview-content",
            classes="preview-content",
        )

    async def show_preview(self, path: str) -> None:
        """Load and display a file preview.

        Uses stat() for size, then read_range() for efficient partial reads.
        """
        header = self.query_one("#preview-header", Static)
        content_widget = self.query_one("#preview-content", Static)

        header.update(f"[dim]{path}[/dim]")
        content_widget.update("[dim]Loading…[/dim]")

        try:
            # Get file metadata first
            stat = await self._fs.stat(path)
            if stat is None:
                content_widget.update("[red]File not found[/red]")
                return

            total_size = stat.get("size", 0)

            # Empty file
            if total_size == 0:
                content_widget.update("[dim]Empty file (0 bytes)[/dim]")
                return

            # Read up to 1MB using range reads
            truncated = total_size > MAX_PREVIEW_BYTES
            if truncated:
                raw = self._fs.read_range(path, 0, MAX_PREVIEW_BYTES)
            else:
                raw = self._fs.read(path)

            # Check for binary
            suffix = PurePosixPath(path).suffix.lower()
            if suffix in _BINARY_EXTENSIONS or _is_likely_binary(raw):
                hex_str = _hex_preview(raw)
                size_str = _format_file_size(total_size)
                content_widget.update(
                    f"[dim]Binary file ({size_str}) — hex preview:[/dim]\n\n{hex_str}\n\n"
                    "[dim]Can't preview binary files[/dim]"
                )
                return

            # Text preview with syntax highlighting
            text = raw.decode("utf-8", errors="replace")
            lexer = _guess_lexer(path)

            from rich.syntax import Syntax

            syntax = Syntax(
                text,
                lexer,
                line_numbers=True,
                theme="monokai",
                word_wrap=True,
            )

            # Build the display
            if truncated:
                size_str = _format_file_size(total_size)
                header.update(
                    f"[dim]{path}[/dim]  [yellow]Showing first 1MB of {size_str}[/yellow]"
                )

            content_widget.update(syntax)

        except Exception as exc:
            content_widget.update(f"[red]Preview error: {format_runtime_error(path, exc)}[/red]")

    def clear_preview(self) -> None:
        """Clear the preview pane."""
        header = self.query_one("#preview-header", Static)
        content_widget = self.query_one("#preview-content", Static)
        header.update("")
        content_widget.update("[dim]Select a file to preview[/dim]")


def _format_file_size(size: int) -> str:
    """Format bytes to human-readable."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"
