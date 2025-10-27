# Parser Development Guide

This guide explains how to create custom parsers for Nexus and integrate them into the system.

## Table of Contents

- [Overview](#overview)
- [Parser Architecture](#parser-architecture)
- [Creating a Custom Parser](#creating-a-custom-parser)
- [Registering and Using Custom Parsers](#registering-and-using-custom-parsers)
- [Advanced Topics](#advanced-topics)
- [Testing Custom Parsers](#testing-custom-parsers)
- [Complete Examples](#complete-examples)

---

## Overview

Nexus includes an extensible parser system that automatically extracts text and structure from various file formats. The default parser is **MarkItDown**, which supports 23+ formats including PDF, Office documents, images, and more.

### Three Ways to Use Nexus Parsers

Nexus provides three interfaces - all using the same underlying parser system:

| Interface | Use Case | Auto-Parse Default | Example |
|-----------|----------|-------------------|---------|
| **CLI** | Command-line operations | ✅ Enabled | `nexus write /file.pdf --input doc.pdf` |
| **`nexus.connect()`** | Standard Python applications | ✅ Enabled | `nx = nexus.connect()` |
| **`Embedded()`** | Advanced/direct control | ✅ Enabled (configurable) | `nx = Embedded(auto_parse=False)` |

All three interfaces support:
- Automatic background parsing on file upload
- Transparent search with `grep()` using parsed text
- Custom parser registration
- Same parser registry and configuration

### Key Features

- **Automatic Parsing**: Files are parsed automatically when uploaded (default: `auto_parse=True`)
- **Transparent Search**: `grep()` automatically searches parsed text instead of raw bytes
- **Extensible**: Easy to add custom parsers for specialized formats
- **Priority System**: Multiple parsers can handle the same format with priority ordering
- **Metadata Integration**: Parsed content stored in Nexus metadata for fast retrieval
- **Background Processing**: Parsing happens in background threads, non-blocking

---

## Parser Architecture

### Core Components

```
nexus.parsers/
├── base.py              # Parser abstract base class
├── types.py             # ParseResult, TextChunk, ImageData
├── registry.py          # ParserRegistry for managing parsers
└── markitdown_parser.py # Default MarkItDown implementation
```

### Data Flow

The parser system works the same across all three interfaces:

```
CLI or Python API
      ↓
1. User uploads file → write()
      ↓
2. Auto-parse triggers → _auto_parse_file() (background thread)
      ↓
3. Registry selects parser → get_parser() (based on extension/MIME type)
      ↓
4. Parser extracts text → parse() (async operation)
      ↓
5. Result stored in metadata → parsed_text, parsed_at, parser_name
      ↓
6. User searches → grep() uses parsed_text (transparent!)
```

**Example Flow:**

```bash
# CLI
$ nexus write /docs/report.pdf --input report.pdf
# → Background: MarkItDown parses PDF → stores text in metadata

$ nexus grep "conclusion" --file-pattern "**/*.pdf"
# → grep() reads parsed_text from metadata → searches text (not binary!)
```

```python
# Python API
nx = nexus.connect()
nx.write("/docs/report.pdf", pdf_bytes)
# → Background: MarkItDown parses PDF → stores text in metadata

results = nx.grep("conclusion", file_pattern="**/*.pdf")
# → grep() reads parsed_text from metadata → searches text (not binary!)
```

### Key Classes

**Parser** (Abstract Base Class)
```python
class Parser(ABC):
    name: str                    # Parser identifier
    priority: int               # Selection priority (higher = preferred)

    @abstractmethod
    def can_parse(self, file_path: str, mime_type: str | None) -> bool:
        """Check if this parser can handle the file."""

    @abstractmethod
    async def parse(self, content: bytes, metadata: dict | None) -> ParseResult:
        """Parse file content and extract text/structure."""

    @property
    @abstractmethod
    def supported_formats(self) -> list[str]:
        """List of file extensions this parser supports."""
```

**ParseResult** (Output Structure)
```python
@dataclass
class ParseResult:
    text: str                              # Extracted plain text
    metadata: dict[str, Any]               # File metadata
    structure: dict[str, Any]              # Document structure (headings, etc.)
    chunks: list[TextChunk]                # Semantic text chunks
    images: list[ImageData]                # Extracted images
    raw_content: str | None                # Original content
```

**ParserRegistry** (Manager)
```python
class ParserRegistry:
    def register(self, parser: Parser) -> None:
        """Register a new parser."""

    def get_parser(self, file_path: str, mime_type: str | None) -> Parser:
        """Get appropriate parser for file (highest priority wins)."""

    def get_supported_formats(self) -> list[str]:
        """List all supported file extensions."""
```

---

## Creating a Custom Parser

### Step 1: Implement the Parser Class

Create a new file for your parser, e.g., `my_custom_parser.py`:

```python
from nexus.parsers.base import Parser
from nexus.parsers.types import ParseResult, TextChunk
from nexus.core.exceptions import ParserError


class MyCustomParser(Parser):
    """Custom parser for .custom file format."""

    def __init__(self, priority: int = 50):
        """
        Initialize parser.

        Args:
            priority: Parser priority (0-100). Higher = preferred when
                     multiple parsers support the same format.
        """
        self.name = "MyCustomParser"
        self.priority = priority

    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        """
        Check if this parser can handle the file.

        Args:
            file_path: Path to the file (used for extension detection)
            mime_type: MIME type of the file (optional)

        Returns:
            True if this parser can handle the file
        """
        # Method 1: Check file extension
        if file_path.endswith('.custom'):
            return True

        # Method 2: Check MIME type
        if mime_type == 'application/x-custom':
            return True

        return False

    async def parse(
        self,
        content: bytes,
        metadata: dict | None = None
    ) -> ParseResult:
        """
        Parse the file content.

        Args:
            content: Raw file bytes
            metadata: Optional file metadata (path, size, mime_type, etc.)

        Returns:
            ParseResult with extracted text and structure

        Raises:
            ParserError: If parsing fails
        """
        try:
            # 1. Decode content
            text = content.decode('utf-8')

            # 2. Extract structure (example: find headers)
            structure = self._extract_structure(text)

            # 3. Create semantic chunks (example: split by paragraphs)
            chunks = self._create_chunks(text)

            # 4. Build metadata
            parse_metadata = metadata or {}
            parse_metadata['parser'] = self.name
            parse_metadata['char_count'] = len(text)

            return ParseResult(
                text=text,
                metadata=parse_metadata,
                structure=structure,
                chunks=chunks,
                images=[],  # Add if you extract images
                raw_content=text,
            )

        except Exception as e:
            path = metadata.get('path', 'unknown') if metadata else 'unknown'
            raise ParserError(
                f"Failed to parse file: {e}",
                path=path,
                parser=self.name,
            ) from e

    @property
    def supported_formats(self) -> list[str]:
        """List of supported file extensions (with leading dot)."""
        return ['.custom', '.cst']

    def _extract_structure(self, text: str) -> dict:
        """Extract document structure (headings, sections, etc.)."""
        structure = {
            'headings': [],
            'sections': [],
        }

        # Example: Find lines starting with '#' as headings
        for i, line in enumerate(text.split('\n')):
            if line.startswith('#'):
                structure['headings'].append({
                    'level': len(line) - len(line.lstrip('#')),
                    'text': line.lstrip('#').strip(),
                    'line': i + 1,
                })

        return structure

    def _create_chunks(self, text: str) -> list[TextChunk]:
        """Split text into semantic chunks."""
        chunks = []

        # Example: Split by double newlines (paragraphs)
        paragraphs = text.split('\n\n')

        for i, para in enumerate(paragraphs):
            if para.strip():
                chunks.append(TextChunk(
                    text=para.strip(),
                    metadata={'chunk_id': i, 'type': 'paragraph'},
                    start_char=text.find(para),
                    end_char=text.find(para) + len(para),
                ))

        return chunks
```

### Step 2: Handle Binary Formats

If your format is binary (not plain text), you'll need additional libraries:

```python
class BinaryFormatParser(Parser):
    """Parser for binary .bin format."""

    def __init__(self):
        self.name = "BinaryFormatParser"
        self.priority = 50

        # Import required library
        try:
            import some_binary_library
            self.lib = some_binary_library
        except ImportError:
            raise ImportError(
                "BinaryFormatParser requires 'some_binary_library'. "
                "Install with: pip install some-binary-library"
            )

    async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
        """Parse binary file."""
        try:
            # Use library to extract text
            doc = self.lib.load(content)
            text = doc.get_text()

            return ParseResult(
                text=text,
                metadata=metadata or {},
            )
        except Exception as e:
            raise ParserError(f"Failed to parse binary file: {e}")

    @property
    def supported_formats(self) -> list[str]:
        return ['.bin', '.dat']
```

---

## Registering and Using Custom Parsers

### Three Ways to Use Nexus Parsers

Nexus provides three interfaces - all share the same underlying parser system:

1. **CLI** - Command-line interface (`nexus write`, `nexus grep`, etc.)
2. **Python API with `nexus.connect()`** - Recommended for applications
3. **Python API with `Embedded()`** - Advanced/direct usage

All three methods support auto-parsing by default!

---

### Method 1: CLI Usage (Auto-Parse Enabled by Default)

**CLI Quick Reference**

Every CLI command supports `--config` option:

```bash
nexus <command> [args] --config path/to/config.yaml

# Examples:
nexus write /file.csv --input data.csv --config my-config.yaml
nexus grep "search" --config my-config.yaml
nexus ls /docs --config my-config.yaml
```

Config file auto-discovery order:
1. `--config` option (if specified)
2. `./nexus.yaml` (current directory)
3. `./nexus.yml` (current directory)
4. `~/.nexus/config.yaml` (home directory)
5. Environment variables (`NEXUS_PARSERS`, `NEXUS_AUTO_PARSE`)

---

The CLI automatically uses parsers - no code needed:

```bash
# Upload a PDF file - auto-parsing happens in background
nexus write /docs/report.pdf --input local-report.pdf

# Wait a moment for parsing to complete
sleep 2

# Search parsed text (not binary!) using grep
nexus grep "conclusion" --file-pattern "**/*.pdf"

# Output:
# Found 3 matches for conclusion:
#
# /docs/report.pdf
#   42: In conclusion, the results demonstrate...
#   Match: conclusion

# Upload any supported format - all auto-parsed
nexus write /data/spreadsheet.xlsx --input data.xlsx
nexus write /docs/presentation.pptx --input slides.pptx

# Search across all parsed documents
nexus grep "TODO"
```

**✅ Config-Based Custom Parsers**

You can now register custom parsers via configuration - **no code needed**!

**Method 1: YAML Configuration File**

Create `nexus.yaml` in your project directory:

```yaml
# nexus.yaml
parsers:
  - module: "my_parsers.csv_parser"
    class: "CSVParser"
    priority: 60
    enabled: true
  - module: "my_parsers.log_parser"
    class: "LogParser"
    priority: 50
    enabled: true

# Control auto-parse behavior
auto_parse: true
```

Then use the CLI:

**Option A: Auto-discovery (looks for nexus.yaml in current directory)**
```bash
# Nexus automatically finds nexus.yaml in current directory
nexus write /data/users.csv --input users.csv
sleep 2  # Wait for auto-parsing
nexus grep "Alice"  # Searches parsed CSV text!
```

**Option B: Explicit config path**
```bash
# Use --config to specify custom config file
nexus write /data/users.csv --input users.csv --config my-config.yaml
nexus grep "Alice" --config my-config.yaml

# Or use absolute path
nexus write /data/users.csv --input users.csv --config /path/to/config.yaml
```

**Option C: Config in home directory**
```bash
# Create ~/.nexus/config.yaml
# Nexus will auto-discover it if no local nexus.yaml exists
mkdir -p ~/.nexus
cat > ~/.nexus/config.yaml <<EOF
parsers:
  - module: "my_parsers.csv_parser"
    class: "CSVParser"
    priority: 60
EOF

# Now all nexus commands use this config
nexus write /data/users.csv --input users.csv
```

**Method 2: Environment Variable**

```bash
# Format: module:class:priority,module:class:priority,...
export NEXUS_PARSERS="my_parsers.csv_parser:CSVParser:60,my_parsers.log_parser:LogParser:50"

# Now use CLI
nexus write /data/users.csv --input users.csv
nexus grep "Alice"
```

**Method 3: Programmatic (Python API)**

```python
import nexus

# Parsers auto-loaded from nexus.yaml or NEXUS_PARSERS env var
nx = nexus.connect()

# Or specify in code
nx = nexus.connect(config={
    "parsers": [
        {"module": "my_parsers.csv_parser", "class": "CSVParser", "priority": 60},
        {"module": "my_parsers.log_parser", "class": "LogParser", "priority": 50},
    ]
})

# Upload and search - parsers work automatically!
with open("users.csv", "rb") as f:
    nx.write("/data/users.csv", f.read())

results = nx.grep("Alice")
```

**Disable Auto-Parse:**

```yaml
# nexus.yaml
auto_parse: false  # Disable automatic parsing
```

Or environment variable:

```bash
export NEXUS_AUTO_PARSE=false
nexus write /docs/report.pdf --input report.pdf  # Won't auto-parse
```

**Parser Configuration Options:**

```yaml
parsers:
  - module: "my_parsers.csv_parser"  # Python module path
    class: "CSVParser"                 # Parser class name
    priority: 60                       # Higher = preferred (default: 50)
    enabled: true                      # Enable/disable (default: true)
```

**How It Works:**

1. Nexus reads `nexus.yaml` (or `NEXUS_PARSERS` env var)
2. Dynamically imports parser modules
3. Instantiates parser classes with specified priority
4. Registers them in the global parser registry
5. CLI/API use parsers automatically - no manual registration needed!

---

### Method 2: Python API with `nexus.connect()` (Recommended)

Use `nexus.connect()` for standard Python applications:

```python
import nexus
from my_custom_parser import MyCustomParser

# Connect to Nexus (auto-detects mode from config)
nx = nexus.connect(config={"data_dir": "./data"})

# Register custom parser
nx.parser_registry.register(MyCustomParser())

# Upload file - auto-parsing happens automatically
with open("document.custom", "rb") as f:
    nx.write("/docs/document.custom", f.read())

# Wait for background parsing
import time
time.sleep(1)

# Search parsed content
results = nx.grep("search term", file_pattern="**/*.custom")
for match in results:
    print(f"{match['file']}:{match['line']} - {match['content']}")

nx.close()
```

**Disable auto-parse:**

```python
# Currently auto_parse is enabled by default in connect()
# To disable, use Embedded() directly (Method 3)
```

---

### Method 3: Direct `Embedded()` Usage (Advanced)

For advanced usage or when you need fine control:

```python
from nexus import Embedded
from my_custom_parser import MyCustomParser

# Create instance with auto-parse disabled
nx = Embedded(data_dir="./data", auto_parse=False)

# Register custom parser
nx.parser_registry.register(MyCustomParser())

# Upload file - NO auto-parsing
with open("document.custom", "rb") as f:
    nx.write("/docs/document.custom", f.read())

# Explicitly parse when needed
import asyncio
result = asyncio.run(nx.parse("/docs/document.custom"))
print(f"Parsed: {result.text[:100]}...")

nx.close()
```

**Or enable auto-parse (default):**

```python
from nexus import Embedded
from my_custom_parser import MyCustomParser

# Auto-parse is True by default
nx = Embedded(data_dir="./data")  # auto_parse=True

# Register custom parser
nx.parser_registry.register(MyCustomParser())

# Upload and auto-parse
with open("document.custom", "rb") as f:
    nx.write("/docs/document.custom", f.read())

# Wait for parsing, then search
import time
time.sleep(1)

results = nx.grep("search term", file_pattern="**/*.custom")
nx.close()
```

### Method 4: Replace Default Parser

Replace MarkItDown with your own parser:

```python
import nexus

# Connect to Nexus
nx = nexus.connect(config={"data_dir": "./data"})

# Remove default MarkItDown parser (optional)
nx.parser_registry._parsers.clear()
nx.parser_registry._format_index.clear()

# Register only your parser
from my_custom_parser import MyCustomParser
nx.parser_registry.register(MyCustomParser())

# Upload file - will use only your parser
with open("document.custom", "rb") as f:
    nx.write("/docs/document.custom", f.read())

nx.close()
```

### Method 5: Multiple Parsers with Priority

Use multiple parsers with priority-based selection:

```python
import nexus
from my_custom_parser import MyCustomParser
from nexus.parsers import MarkItDownParser

nx = nexus.connect(config={"data_dir": "./data"})

# Register custom parser with HIGHER priority than MarkItDown (default: 50)
nx.parser_registry.register(MyCustomParser(priority=100))

# For .custom files, MyCustomParser will be used (priority 100 > 50)
# For .pdf files, MarkItDownParser will still be used

nx.close()
```

### Method 6: Explicit Parsing (No Auto-Parse)

Manually control when parsing happens:

```python
import asyncio
import nexus
from my_custom_parser import MyCustomParser

async def main():
    # Use Embedded directly to disable auto-parse
    from nexus import Embedded
    nx = Embedded(data_dir="./data", auto_parse=False)

    # Register parser
    nx.parser_registry.register(MyCustomParser())

    # Upload file (no automatic parsing)
    with open("document.custom", "rb") as f:
        nx.write("/docs/document.custom", f.read())

    # Explicitly parse when needed
    result = await nx.parse("/docs/document.custom")

    print(f"Extracted text: {result.text[:100]}...")
    print(f"Structure: {result.structure}")
    print(f"Chunks: {len(result.chunks)}")

    nx.close()

asyncio.run(main())
```

---

## Advanced Topics

### Priority-Based Parser Selection

When multiple parsers support the same format, the registry selects based on priority:

```python
# High priority (100) - used for specialized handling
fs.parser_registry.register(CustomPDFParser(priority=100))

# Default priority (50) - MarkItDown
fs.parser_registry.register(MarkItDownParser(priority=50))

# Low priority (10) - fallback
fs.parser_registry.register(GenericTextParser(priority=10))
```

### MIME Type Detection

Use MIME types for more accurate format detection:

```python
class SmartParser(Parser):
    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        # Prefer MIME type over extension
        if mime_type:
            return mime_type in [
                'application/x-custom',
                'text/x-custom',
            ]

        # Fallback to extension
        return file_path.endswith('.custom')
```

### Async Operations

Parsers can use async/await for I/O operations:

```python
class AsyncParser(Parser):
    async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
        # Async operations (network calls, subprocess, etc.)
        import aiofiles

        # Save to temp file for processing
        async with aiofiles.open('/tmp/temp.bin', 'wb') as f:
            await f.write(content)

        # Process asynchronously
        text = await self._process_async('/tmp/temp.bin')

        return ParseResult(text=text, metadata=metadata or {})

    async def _process_async(self, path: str) -> str:
        # Async processing logic
        await asyncio.sleep(0.1)  # Simulate work
        return "processed text"
```

### Error Handling

Always raise `ParserError` for parse failures:

```python
from nexus.core.exceptions import ParserError

class SafeParser(Parser):
    async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
        try:
            text = self._risky_operation(content)
            return ParseResult(text=text, metadata=metadata or {})
        except UnicodeDecodeError as e:
            raise ParserError(
                "Invalid encoding - file may be corrupted",
                path=metadata.get('path') if metadata else None,
                parser=self.name,
            ) from e
        except Exception as e:
            raise ParserError(
                f"Unexpected error: {e}",
                path=metadata.get('path') if metadata else None,
                parser=self.name,
            ) from e
```

### Conditional Dependencies

Make parser dependencies optional:

```python
class OptionalLibParser(Parser):
    def __init__(self):
        self.name = "OptionalLibParser"
        self.priority = 50
        self._lib = None

    @property
    def lib(self):
        """Lazy-load library."""
        if self._lib is None:
            try:
                import optional_library
                self._lib = optional_library
            except ImportError:
                raise ImportError(
                    f"{self.name} requires 'optional_library'. "
                    "Install with: pip install optional-library"
                )
        return self._lib

    async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
        # Library loaded only when parse() is called
        doc = self.lib.parse(content)
        return ParseResult(text=doc.text, metadata=metadata or {})
```

---

## Testing Custom Parsers

### Unit Tests

Create tests in `tests/unit/parsers/test_my_custom_parser.py`:

```python
import pytest
from my_custom_parser import MyCustomParser
from nexus.parsers.types import ParseResult
from nexus.core.exceptions import ParserError


@pytest.fixture
def parser():
    """Provide parser instance for tests."""
    return MyCustomParser()


def test_supported_formats(parser: MyCustomParser):
    """Test supported format listing."""
    formats = parser.supported_formats
    assert '.custom' in formats
    assert '.cst' in formats


def test_can_parse_by_extension(parser: MyCustomParser):
    """Test format detection by file extension."""
    assert parser.can_parse("file.custom")
    assert parser.can_parse("file.cst")
    assert not parser.can_parse("file.txt")


def test_can_parse_by_mime_type(parser: MyCustomParser):
    """Test format detection by MIME type."""
    assert parser.can_parse("file.unknown", mime_type="application/x-custom")
    assert not parser.can_parse("file.unknown", mime_type="text/plain")


@pytest.mark.asyncio
async def test_parse_simple_file(parser: MyCustomParser):
    """Test parsing basic content."""
    content = b"Hello world\n\nThis is a test."
    result = await parser.parse(content, metadata={"path": "/test.custom"})

    assert isinstance(result, ParseResult)
    assert "Hello world" in result.text
    assert result.metadata.get("parser") == "MyCustomParser"


@pytest.mark.asyncio
async def test_parse_with_structure(parser: MyCustomParser):
    """Test structure extraction."""
    content = b"# Heading 1\nContent\n\n## Heading 2\nMore content"
    result = await parser.parse(content)

    assert len(result.structure.get('headings', [])) == 2
    assert result.structure['headings'][0]['level'] == 1
    assert result.structure['headings'][0]['text'] == "Heading 1"


@pytest.mark.asyncio
async def test_parse_creates_chunks(parser: MyCustomParser):
    """Test chunk creation."""
    content = b"Paragraph 1\n\nParagraph 2\n\nParagraph 3"
    result = await parser.parse(content)

    assert len(result.chunks) == 3
    assert result.chunks[0].text == "Paragraph 1"


@pytest.mark.asyncio
async def test_parse_empty_file(parser: MyCustomParser):
    """Test parsing empty content."""
    content = b""
    result = await parser.parse(content)

    # Should return empty result, not crash
    assert result.text == ""


@pytest.mark.asyncio
async def test_parse_invalid_utf8(parser: MyCustomParser):
    """Test parsing invalid UTF-8 raises ParserError."""
    content = b'\xff\xfe'  # Invalid UTF-8

    with pytest.raises(ParserError) as exc_info:
        await parser.parse(content, metadata={"path": "/test.custom"})

    assert "MyCustomParser" in str(exc_info.value)
    assert exc_info.value.path == "/test.custom"


@pytest.mark.asyncio
async def test_parse_metadata_preserved(parser: MyCustomParser):
    """Test that input metadata is preserved."""
    content = b"Test content"
    metadata = {
        "path": "/test.custom",
        "size": len(content),
        "mime_type": "application/x-custom",
    }

    result = await parser.parse(content, metadata=metadata)

    assert result.metadata["path"] == "/test.custom"
    assert result.metadata["size"] == len(content)
    assert result.metadata["mime_type"] == "application/x-custom"
```

### Integration Tests

Test with actual Nexus instance in `tests/integration/test_custom_parser_integration.py`:

```python
import pytest
import asyncio
from nexus import Embedded
from my_custom_parser import MyCustomParser


@pytest.fixture
def fs(tmp_path):
    """Provide Nexus instance with custom parser."""
    fs = Embedded(data_dir=str(tmp_path), auto_parse=True)
    fs.parser_registry.register(MyCustomParser())
    yield fs
    fs.close()


def test_auto_parse_on_write(fs: Embedded):
    """Test automatic parsing when file is uploaded."""
    content = b"# Test Document\n\nThis is test content."
    fs.write("/test.custom", content)

    # Wait for background parsing
    import time
    time.sleep(0.5)

    # Check parsed text was stored in metadata
    parsed = fs.metadata.get_file_metadata("/test.custom", "parsed_text")
    assert parsed is not None
    assert "Test Document" in parsed


def test_grep_searches_parsed_text(fs: Embedded):
    """Test grep() uses parsed text."""
    content = b"# Important Document\n\nSecret information here."
    fs.write("/docs/secret.custom", content)

    # Wait for parsing
    import time
    time.sleep(0.5)

    # Search should find text in parsed content
    results = fs.grep("Secret", file_pattern="**/*.custom")
    assert len(results) > 0
    assert results[0]['path'] == "/docs/secret.custom"


@pytest.mark.asyncio
async def test_explicit_parse(fs: Embedded):
    """Test explicit parse() call."""
    content = b"Test content for explicit parsing"
    fs.write("/test.custom", content)

    # Parse explicitly
    result = await fs.parse("/test.custom")

    assert "Test content" in result.text
    assert result.metadata.get("parser") == "MyCustomParser"


def test_format_detection(fs: Embedded):
    """Test parser is selected correctly."""
    parser = fs.parser_registry.get_parser("file.custom")
    assert parser.name == "MyCustomParser"

    formats = fs.parser_registry.get_supported_formats()
    assert ".custom" in formats
```

### Running Tests

```bash
# Run all parser tests
pytest tests/unit/parsers/test_my_custom_parser.py -v

# Run with coverage
pytest tests/unit/parsers/test_my_custom_parser.py --cov=my_custom_parser --cov-report=term-missing

# Run integration tests
pytest tests/integration/test_custom_parser_integration.py -v
```

---

## Complete Examples

### Example 1: CSV Parser

```python
import csv
from io import StringIO
from nexus.parsers.base import Parser
from nexus.parsers.types import ParseResult, TextChunk
from nexus.core.exceptions import ParserError


class CSVParser(Parser):
    """Parser for CSV files with enhanced metadata extraction."""

    def __init__(self, priority: int = 60):
        self.name = "CSVParser"
        self.priority = priority

    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        if mime_type in ['text/csv', 'application/csv']:
            return True
        return file_path.endswith('.csv')

    async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
        try:
            # Decode and parse CSV
            text = content.decode('utf-8')
            reader = csv.DictReader(StringIO(text))
            rows = list(reader)

            # Extract structure
            structure = {
                'columns': reader.fieldnames or [],
                'row_count': len(rows),
                'has_header': bool(reader.fieldnames),
            }

            # Create chunks (one per row)
            chunks = []
            for i, row in enumerate(rows):
                row_text = ', '.join(f"{k}: {v}" for k, v in row.items())
                chunks.append(TextChunk(
                    text=row_text,
                    metadata={'row_number': i + 1, 'type': 'csv_row'},
                ))

            # Create searchable text representation
            searchable_text = '\n'.join(chunk.text for chunk in chunks)

            # Enhanced metadata
            parse_metadata = metadata or {}
            parse_metadata.update({
                'parser': self.name,
                'row_count': len(rows),
                'column_count': len(reader.fieldnames or []),
                'columns': reader.fieldnames or [],
            })

            return ParseResult(
                text=searchable_text,
                metadata=parse_metadata,
                structure=structure,
                chunks=chunks,
                raw_content=text,
            )

        except Exception as e:
            raise ParserError(
                f"Failed to parse CSV: {e}",
                path=metadata.get('path') if metadata else None,
                parser=self.name,
            ) from e

    @property
    def supported_formats(self) -> list[str]:
        return ['.csv']
```

### Example 2: Log File Parser

```python
import re
from datetime import datetime
from nexus.parsers.base import Parser
from nexus.parsers.types import ParseResult, TextChunk
from nexus.core.exceptions import ParserError


class LogParser(Parser):
    """Parser for application log files."""

    # Common log level patterns
    LOG_PATTERN = re.compile(
        r'(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+'
        r'\[(?P<level>\w+)\]\s+'
        r'(?P<message>.*)',
        re.MULTILINE
    )

    def __init__(self, priority: int = 50):
        self.name = "LogParser"
        self.priority = priority

    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        return file_path.endswith(('.log', '.logs'))

    async def parse(self, content: bytes, metadata: dict | None = None) -> ParseResult:
        try:
            text = content.decode('utf-8', errors='replace')

            # Parse log entries
            entries = []
            chunks = []

            for match in self.LOG_PATTERN.finditer(text):
                entry = {
                    'timestamp': match.group('timestamp'),
                    'level': match.group('level'),
                    'message': match.group('message'),
                }
                entries.append(entry)

                # Create chunk for each log entry
                chunks.append(TextChunk(
                    text=f"[{entry['level']}] {entry['message']}",
                    metadata=entry,
                    start_char=match.start(),
                    end_char=match.end(),
                ))

            # Extract structure/statistics
            level_counts = {}
            for entry in entries:
                level = entry['level']
                level_counts[level] = level_counts.get(level, 0) + 1

            structure = {
                'total_entries': len(entries),
                'level_counts': level_counts,
                'first_timestamp': entries[0]['timestamp'] if entries else None,
                'last_timestamp': entries[-1]['timestamp'] if entries else None,
            }

            # Enhanced metadata
            parse_metadata = metadata or {}
            parse_metadata.update({
                'parser': self.name,
                'log_entries': len(entries),
                'error_count': level_counts.get('ERROR', 0),
                'warning_count': level_counts.get('WARNING', 0),
            })

            return ParseResult(
                text=text,
                metadata=parse_metadata,
                structure=structure,
                chunks=chunks,
                raw_content=text,
            )

        except Exception as e:
            raise ParserError(
                f"Failed to parse log file: {e}",
                path=metadata.get('path') if metadata else None,
                parser=self.name,
            ) from e

    @property
    def supported_formats(self) -> list[str]:
        return ['.log', '.logs']
```

### Example 3: Using Both Custom Parsers

**Python API:**

```python
import asyncio
import nexus
from csv_parser import CSVParser
from log_parser import LogParser


async def main():
    # Connect and register parsers
    nx = nexus.connect(config={"data_dir": "./data"})
    nx.parser_registry.register(CSVParser(priority=60))
    nx.parser_registry.register(LogParser(priority=50))

    # Upload CSV file
    csv_content = b"name,age,city\nAlice,30,NYC\nBob,25,LA"
    nx.write("/data/users.csv", csv_content)

    # Upload log file
    log_content = b"""2024-01-15 10:30:00 [INFO] Application started
2024-01-15 10:30:05 [ERROR] Connection failed
2024-01-15 10:30:10 [WARNING] Retrying connection"""
    nx.write("/logs/app.log", log_content)

    # Wait for parsing
    await asyncio.sleep(1)

    # Search across both file types
    results = nx.grep("ERROR")
    for result in results:
        print(f"Found in: {result['path']}")
        print(f"Line: {result['line']}")
        print()

    # Get CSV structure
    csv_result = await nx.parse("/data/users.csv")
    print(f"CSV columns: {csv_result.structure['columns']}")
    print(f"CSV rows: {csv_result.structure['row_count']}")

    # Get log statistics
    log_result = await nx.parse("/logs/app.log")
    print(f"Log entries: {log_result.structure['total_entries']}")
    print(f"Level counts: {log_result.structure['level_counts']}")

    nx.close()


if __name__ == "__main__":
    asyncio.run(main())
```

**CLI:**

```bash
# Upload CSV and log files (auto-parsed)
echo "name,age,city\nAlice,30,NYC\nBob,25,LA" | nexus write /data/users.csv --input -
cat app.log | nexus write /logs/app.log --input -

# Wait for parsing
sleep 2

# Search across both file types
nexus grep "ERROR"

# Output:
# Found 1 matches for ERROR:
#
# /logs/app.log
#   2: 2024-01-15 10:30:05 [ERROR] Connection failed
#   Match: ERROR

# List all log files
nexus ls /logs

# View parsed content
nexus cat /data/users.csv  # Shows CSV content
```

---

## Best Practices

### 1. Always Handle Errors Gracefully

```python
try:
    result = await parser.parse(content)
except ParserError as e:
    # Log error but don't crash
    logger.error(f"Parse failed: {e}")
    return None
```

### 2. Validate Input Before Parsing

```python
def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
    # Check file size limit
    if metadata and metadata.get('size', 0) > 100_000_000:  # 100 MB
        return False

    return file_path.endswith('.custom')
```

### 3. Use Lazy Loading for Heavy Dependencies

```python
@property
def heavy_lib(self):
    if not hasattr(self, '_heavy_lib'):
        import heavy_library
        self._heavy_lib = heavy_library
    return self._heavy_lib
```

### 4. Preserve Original Metadata

```python
parse_metadata = metadata.copy() if metadata else {}
parse_metadata['parser'] = self.name
# Don't overwrite existing keys
```

### 5. Document Supported Formats Clearly

```python
@property
def supported_formats(self) -> list[str]:
    """
    Supported formats:
    - .custom: Custom text format
    - .cst: Custom binary format
    """
    return ['.custom', '.cst']
```

---

## Troubleshooting

### Parser Not Being Used

**Problem**: Custom parser registered but not being used.

**Solution**:
1. Check `can_parse()` returns `True` for your files
2. Verify file extension matches `supported_formats`
3. Check parser priority (higher = preferred)

**Python API:**
```python
# Debug parser selection
parser = nx.parser_registry.get_parser("file.custom")
print(f"Selected parser: {parser.name}")
```

**CLI:**
```bash
# Check if format is supported
# (No direct CLI command yet - use Python for debugging)
python -c "
import nexus
nx = nexus.connect()
parser = nx.parser_registry.get_parser('file.custom')
print(f'Selected parser: {parser.name}')
"
```

### Parsing Fails Silently

**Problem**: Files uploaded but `parsed_text` not in metadata.

**Solution**:
1. Check `auto_parse=True`
2. Wait longer for background parsing (increase sleep time)
3. Check for errors in logs
4. Try explicit parse to see error

**Python API:**
```python
# Use Embedded to access metadata directly
from nexus import Embedded
nx = Embedded(data_dir="./data")

# Check if file was parsed
parsed_text = nx.metadata.get_file_metadata("/path/to/file.custom", "parsed_text")
if parsed_text:
    print(f"Parsed successfully: {len(parsed_text)} characters")
else:
    print("Not parsed yet or parsing failed")

# Try explicit parse to see errors
import asyncio
try:
    result = asyncio.run(nx.parse("/path/to/file.custom"))
    print(f"Success: {result.text[:100]}")
except Exception as e:
    print(f"Parse error: {e}")
```

**CLI:**
```bash
# Check if grep finds content in parsed text
nexus grep "test" --file-pattern "**/*.custom"

# If no results, file may not be parsed yet
# Wait and try again
sleep 3
nexus grep "test" --file-pattern "**/*.custom"
```

### Import Errors

**Problem**: `ModuleNotFoundError` when using parser.

**Solution**:
1. Install required dependencies: `pip install required-library`
2. Use lazy imports in parser
3. Document dependencies clearly

---

## Resources

- **Parser Base Classes**: `src/nexus/parsers/base.py`
- **Data Types**: `src/nexus/parsers/types.py`
- **MarkItDown Example**: `src/nexus/parsers/markitdown_parser.py`
- **Tests**: `tests/unit/parsers/`
- **User Guide**: `PARSER_INTEGRATION.md`

---

## Future Enhancements

### Planned for v0.2.1+

**✅ Config-Based Parser Loading** - **IMPLEMENTED in v0.2.0!** See "Method 1: CLI Usage" above.

**✅ Auto-Parse Configuration** - **IMPLEMENTED in v0.2.0!** Use `auto_parse: true/false` in nexus.yaml or `NEXUS_AUTO_PARSE` env var.

**🔜 Advanced Auto-Parse Control** (planned):

```yaml
# nexus.yaml
auto_parse: true
auto_parse_formats:  # Only auto-parse specific formats
  - .pdf
  - .docx
  - .csv
auto_parse_exclude:  # Exclude specific paths
  - /temp/*
  - /cache/*
```

**🔜 Parser Plugin System** (planned):

Auto-discover parsers from a plugins directory:

```
~/.nexus/plugins/
  csv_parser/
    __init__.py
    parser.py  # Contains CSVParser class
  log_parser/
    __init__.py
    parser.py  # Contains LogParser class
```

Nexus will automatically load parsers from `~/.nexus/plugins/` on startup.

**🔜 Parser Priority Override** (planned):

Allow priority override without modifying parser code:

```yaml
# nexus.yaml
parser_priorities:
  MarkItDownParser: 30  # Lower priority for default
  CSVParser: 100  # Higher priority for CSV files
```

### How to Request Features

If you need any of these features sooner, please:
1. Open an issue at https://github.com/nexi-lab/nexus/issues
2. Describe your use case
3. Vote on existing feature requests

---

## Contributing

When contributing a new parser to Nexus:

1. Follow the architecture described in this guide
2. Write comprehensive unit tests (aim for 90%+ coverage)
3. Include integration tests with Embedded
4. Document supported formats and dependencies
5. Update `pyproject.toml` with required packages
6. Add example usage to `examples/` directory
7. Consider whether it should be a built-in parser or a plugin

### Contributing Built-in Parsers

For parsers that should be included in Nexus core:
- Must support common file formats (CSV, JSON, XML, etc.)
- Must have minimal dependencies
- Must have comprehensive tests
- Submit PR to main repository

### Contributing Parser Plugins

For specialized parsers (domain-specific formats):
- Create as a separate package
- Follow naming convention: `nexus-parser-{format}`
- Publish to PyPI
- Submit to parser registry (when available)

**Questions?** Open an issue at https://github.com/nexi-lab/nexus/issues
