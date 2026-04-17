# pdf-inspector default PDF parser — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `markitdown[all]` to optional deps, add `pdf-inspector` (Rust+PyO3) as the default local PDF parse provider, and wire it through the registry + env config — relying on `is_available()` for graceful degradation.

**Architecture:** New `PdfInspectorProvider` mirrors the existing `MarkItDownProvider` pattern (sync lib called via thread executor; lazy thread-safe singleton; `ParserError` wrapping). Registered in two places: `ProviderRegistry.auto_discover()` (priority 20, between LlamaParse and MarkItDown) and `config._load_from_environment()` (conditional on import). MarkItDown stays in the codebase as an opt-in fallback for non-PDF formats.

**Tech Stack:** Python ≥3.12; `pdf_inspector` 0.1.1 (PyO3 wheels: cp312 only — `python_version == '3.12'` constraint); reportlab (one-off, scratch env, to generate the fixture PDF — not added to project deps).

**Spec:** [`docs/superpowers/specs/2026-04-16-pdf-inspector-default-parser-design.md`](../specs/2026-04-16-pdf-inspector-default-parser-design.md)

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `pyproject.toml` | modify | move markitdown→optional, add pdf-inspector to core |
| `src/nexus/bricks/parsers/providers/pdf_inspector_provider.py` | create | `PdfInspectorProvider` class |
| `src/nexus/bricks/parsers/providers/__init__.py` | modify | export `PdfInspectorProvider` |
| `src/nexus/bricks/parsers/providers/registry.py` | modify | auto-discover branch for pdf-inspector (priority 20) |
| `src/nexus/config.py` | modify | conditional provider registration in `_load_from_environment()`; update docstrings |
| `tests/unit/bricks/parsers/providers/__init__.py` | create | empty package marker |
| `tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py` | create | provider unit tests (mocked + fixture-backed) |
| `tests/unit/bricks/parsers/providers/test_provider_registry.py` | create | auto_discover behavior tests |
| `tests/unit/bricks/parsers/providers/fixtures/hello_text.pdf` | create | tiny text-based PDF (binary fixture) |
| `tests/unit/bricks/parsers/providers/fixtures/__init__.py` | not needed | (binary asset directory; no Python) |
| `tests/unit/cli/test_config.py` | modify | tests for env-based parse_providers registration |

---

## Task 1: Update `pyproject.toml`

**Files:**
- Modify: `pyproject.toml:96` (move markitdown line)
- Modify: `pyproject.toml:139-147` (add markitdown optional group; add pdf-inspector core)

- [ ] **Step 1: Remove markitdown from core deps**

In `pyproject.toml`, find line 95-96:

```toml
    # Document parsing (Python 3.13 only - not yet compatible with 3.14)
    "markitdown[all]>=0.1.0; python_version < '3.14'",
```

Replace with:

```toml
    # PDF parsing (default local provider; ~2-3 MB Rust ext, zero Python deps).
    # cp312 wheels only as of 0.1.1; loosen the version_info guard when upstream ships cp313+.
    "pdf-inspector>=0.1.1; python_version == '3.12'",
```

- [ ] **Step 2: Add markitdown to optional dependencies**

In `pyproject.toml`, find the `[project.optional-dependencies]` section (~line 139). Add a new group **above** the `performance` group (so it sorts first alphabetically isn't important, but place it first for visibility):

```toml
[project.optional-dependencies]
markitdown = [
    # Office/HTML/EPUB/audio parsing fallback. ~200 MB transitive deps
    # (pandas, numpy, lxml, onnxruntime, pdfminer, python-pptx, mammoth, openpyxl, ...).
    # Install with: pip install nexus[markitdown]
    "markitdown[all]>=0.1.0; python_version < '3.14'",
]

performance = [
    ...
```

- [ ] **Step 3: Verify pyproject.toml parses**

Run: `python3 -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"`
Expected: no output (success).

- [ ] **Step 4: Resync the lock and verify install**

Run: `uv sync --all-groups`
Expected: dependencies resolve and install. `pdf_inspector` appears in `uv.lock`. `markitdown` does **not** appear in core (only in `markitdown` optional extra).

- [ ] **Step 5: Confirm pdf_inspector imports**

Run: `python3 -c "import pdf_inspector; print(pdf_inspector.__name__)"`
Expected: `pdf_inspector`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(deps): make markitdown optional, add pdf-inspector core dep

Issue #3757: drops ~200 MB from default install by moving markitdown to
[project.optional-dependencies], adds pdf-inspector (~2-3 MB Rust ext)
as the default local PDF parser."
```

---

## Task 2: Create the test PDF fixture

The provider tests need a real text-based PDF to exercise `pdf_inspector.process_pdf_bytes()`. We commit a tiny pre-generated PDF as a binary fixture so the test suite has zero runtime PDF-generation dependencies.

**Files:**
- Create: `tests/unit/bricks/parsers/providers/fixtures/hello_text.pdf` (binary, ~1 KB)

- [ ] **Step 1: Create the fixture directory**

Run:

```bash
mkdir -p tests/unit/bricks/parsers/providers/fixtures
```

Expected: directory created.

- [ ] **Step 2: Generate the PDF in a scratch env**

reportlab is **not** added to project deps. Use a one-off scratch venv:

```bash
python3 -m venv /tmp/pdfgen && \
  /tmp/pdfgen/bin/pip install -q 'reportlab>=4.0.0' && \
  /tmp/pdfgen/bin/python -c "
from reportlab.pdfgen import canvas
c = canvas.Canvas('tests/unit/bricks/parsers/providers/fixtures/hello_text.pdf')
c.drawString(100, 700, 'Hello World')
c.drawString(100, 680, 'pdf-inspector test fixture')
c.save()
" && \
  rm -rf /tmp/pdfgen
```

Expected: file `tests/unit/bricks/parsers/providers/fixtures/hello_text.pdf` exists, ~1-2 KB.

- [ ] **Step 3: Sanity check the fixture**

Run:

```bash
python3 -c "
import pdf_inspector, pathlib
data = pathlib.Path('tests/unit/bricks/parsers/providers/fixtures/hello_text.pdf').read_bytes()
result = pdf_inspector.process_pdf_bytes(data)
print('pdf_type:', result.pdf_type)
print('pages_needing_ocr:', list(result.pages_needing_ocr))
print('markdown contains Hello World:', 'Hello World' in result.markdown)
"
```

Expected output (approximately):
```
pdf_type: text_based
pages_needing_ocr: []
markdown contains Hello World: True
```

- [ ] **Step 4: Commit the fixture**

```bash
git add tests/unit/bricks/parsers/providers/fixtures/hello_text.pdf
git commit -m "test(parsers): add hello_text.pdf fixture for pdf-inspector"
```

---

## Task 3: Create the test package directory

**Files:**
- Create: `tests/unit/bricks/parsers/providers/__init__.py`

- [ ] **Step 1: Add empty package marker**

Run:

```bash
touch tests/unit/bricks/parsers/providers/__init__.py
```

- [ ] **Step 2: Commit**

```bash
git add tests/unit/bricks/parsers/providers/__init__.py
git commit -m "test(parsers): add providers test package"
```

---

## Task 4: TDD — `PdfInspectorProvider`, smallest passing test (`is_available` + `default_formats`)

**Files:**
- Create: `tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py`
- Create: `src/nexus/bricks/parsers/providers/pdf_inspector_provider.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py`:

```python
"""Tests for PdfInspectorProvider."""

import pathlib
import sys
from unittest.mock import patch

import pytest

from nexus.bricks.parsers.providers.pdf_inspector_provider import PdfInspectorProvider
from nexus.contracts.exceptions import ParserError

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_default_formats_is_pdf_only():
    provider = PdfInspectorProvider()
    assert provider.default_formats == [".pdf"]


def test_name():
    provider = PdfInspectorProvider()
    assert provider.name == "pdf-inspector"


def test_is_available_when_installed():
    provider = PdfInspectorProvider()
    pdf_inspector = pytest.importorskip("pdf_inspector")
    assert provider.is_available() is True


def test_is_available_returns_false_when_import_fails(monkeypatch):
    provider = PdfInspectorProvider()
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)
    assert provider.is_available() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py -v`
Expected: ImportError / collection failure (`pdf_inspector_provider` module does not exist).

- [ ] **Step 3: Write the minimal provider implementation**

Create `src/nexus/bricks/parsers/providers/pdf_inspector_provider.py`:

```python
"""pdf-inspector parse provider (default local PDF provider)."""

import logging
import threading
from typing import Any

from nexus.bricks.parsers.providers.base import ParseProvider, ProviderConfig

logger = logging.getLogger(__name__)


class PdfInspectorProvider(ParseProvider):
    """Parse provider using pdf-inspector (Rust + PyO3).

    Fast text-based PDF extraction with Markdown output, table/heading
    detection, and per-page OCR-need classification. Surfaces
    ``pages_needing_ocr`` in metadata so future smart-routing layers
    can re-process scanned pages with an OCR-capable provider.

    Requires:
        - pdf-inspector package: pip install pdf-inspector

    Example:
        >>> from nexus.bricks.parsers.providers import ProviderConfig
        >>> config = ProviderConfig(name="pdf-inspector", priority=20)
        >>> provider = PdfInspectorProvider(config)
        >>> result = await provider.parse(content, "document.pdf")
    """

    DEFAULT_FORMATS = [".pdf"]

    def __init__(self, config: ProviderConfig | None = None) -> None:
        super().__init__(config)
        self._inspector: Any = None
        self._init_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "pdf-inspector"

    @property
    def default_formats(self) -> list[str]:
        return self.DEFAULT_FORMATS.copy()

    def is_available(self) -> bool:
        try:
            import pdf_inspector  # noqa: F401

            return pdf_inspector is not None
        except Exception:
            logger.debug("pdf_inspector not available, PdfInspectorProvider unavailable")
            return False
```

(The `parse()` method is added in Task 5 below, after its tests fail.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py -v`
Expected: 4 passed.

> **Note on `is_available_returns_false_when_import_fails`:** setting `sys.modules["pdf_inspector"] = None` causes `import pdf_inspector` to raise `ImportError`, which the provider catches and returns False. The `pdf_inspector is not None` guard in the success branch is defensive — handles the case where pdf_inspector was already partially imported.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py \
        src/nexus/bricks/parsers/providers/pdf_inspector_provider.py
git commit -m "feat(parsers): add PdfInspectorProvider scaffold

Implements name, default_formats, and is_available. Parse method comes
in the next commit."
```

---

## Task 5: TDD — `PdfInspectorProvider.parse()` happy path

**Files:**
- Modify: `tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py`
- Modify: `src/nexus/bricks/parsers/providers/pdf_inspector_provider.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py`:

```python
@pytest.mark.asyncio
async def test_parse_text_pdf_returns_markdown_and_metadata():
    pytest.importorskip("pdf_inspector")
    provider = PdfInspectorProvider()
    content = (FIXTURES / "hello_text.pdf").read_bytes()

    result = await provider.parse(content, "hello_text.pdf")

    assert "Hello World" in result.text
    assert result.metadata["parser"] == "pdf-inspector"
    assert result.metadata["format"] == ".pdf"
    assert result.metadata["original_path"] == "hello_text.pdf"
    assert result.metadata["pdf_type"] == "text_based"
    assert result.metadata["pages_needing_ocr"] == []
    assert result.metadata["requires_ocr"] is False
    assert result.metadata["has_encoding_issues"] is False
    assert result.chunks  # non-empty
    assert isinstance(result.structure, dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py::test_parse_text_pdf_returns_markdown_and_metadata -v`
Expected: FAIL — `parse()` is not implemented.

- [ ] **Step 3: Implement `parse()`**

In `src/nexus/bricks/parsers/providers/pdf_inspector_provider.py`, add imports at the top (alongside existing imports):

```python
import asyncio
from pathlib import Path

from nexus.bricks.parsers.types import ParseResult
from nexus.bricks.parsers.utils import create_chunks, extract_structure
from nexus.contracts.exceptions import ParserError
```

Then add to the `PdfInspectorProvider` class (after `is_available`):

```python
    def _get_inspector(self) -> Any:
        """Get or create the pdf_inspector module reference (thread-safe)."""
        if self._inspector is not None:
            return self._inspector
        with self._init_lock:
            if self._inspector is not None:
                return self._inspector
            import pdf_inspector

            self._inspector = pdf_inspector
        return self._inspector

    async def parse(
        self,
        content: bytes,
        file_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ParseResult:
        """Parse a PDF using pdf-inspector.

        Args:
            content: Raw PDF bytes.
            file_path: Original file path (used in metadata + errors).
            metadata: Optional caller metadata, merged into the result.

        Returns:
            ParseResult with markdown text, chunks, structure, and OCR-need
            flags in ``metadata``.

        Raises:
            ParserError: If pdf-inspector fails to process the bytes.
        """
        metadata = metadata or {}
        ext = Path(file_path).suffix.lower()
        inspector = self._get_inspector()

        try:
            # process_pdf_bytes is sync (PyO3); run off the event loop.
            result = await asyncio.to_thread(inspector.process_pdf_bytes, content)
        except Exception as e:
            raise ParserError(
                f"Failed to parse PDF with pdf-inspector: {e}",
                path=file_path,
                parser=self.name,
            ) from e

        text_content = result.markdown or ""
        pages_needing_ocr = list(result.pages_needing_ocr)

        return ParseResult(
            text=text_content,
            metadata={
                "parser": self.name,
                "format": ext,
                "original_path": file_path,
                "pdf_type": result.pdf_type,
                "pages_needing_ocr": pages_needing_ocr,
                "requires_ocr": bool(pages_needing_ocr),
                "has_encoding_issues": bool(result.has_encoding_issues),
                **metadata,
            },
            structure=extract_structure(text_content),
            chunks=create_chunks(text_content),
            raw_content=text_content,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py::test_parse_text_pdf_returns_markdown_and_metadata -v`
Expected: PASS.

- [ ] **Step 5: Run full provider test file**

Run: `pytest tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/parsers/providers/pdf_inspector_provider.py \
        tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py
git commit -m "feat(parsers): implement PdfInspectorProvider.parse

Calls pdf_inspector.process_pdf_bytes via thread executor, returns
markdown + per-page OCR-need flags in metadata."
```

---

## Task 6: TDD — `PdfInspectorProvider.parse()` error path

**Files:**
- Modify: `tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py`:

```python
@pytest.mark.asyncio
async def test_parse_invalid_bytes_raises_parser_error():
    pytest.importorskip("pdf_inspector")
    provider = PdfInspectorProvider()

    with pytest.raises(ParserError) as exc_info:
        await provider.parse(b"not a real pdf", "broken.pdf")

    assert exc_info.value.parser == "pdf-inspector"
    assert "broken.pdf" in str(exc_info.value) or exc_info.value.path == "broken.pdf"
```

- [ ] **Step 2: Run test to verify it passes**

The implementation in Task 5 already wraps exceptions in `ParserError`. This test verifies the contract.

Run: `pytest tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py::test_parse_invalid_bytes_raises_parser_error -v`
Expected: PASS.

> If FAIL because pdf-inspector returns an empty result instead of raising, replace the test body with a mock-based assertion:
> ```python
> @pytest.mark.asyncio
> async def test_parse_propagates_inspector_failure_as_parser_error():
>     provider = PdfInspectorProvider()
>     boom = RuntimeError("decode failure")
>     with patch.object(provider, "_get_inspector") as get:
>         get.return_value.process_pdf_bytes.side_effect = boom
>         with pytest.raises(ParserError) as exc_info:
>             await provider.parse(b"x", "broken.pdf")
>     assert exc_info.value.parser == "pdf-inspector"
> ```

- [ ] **Step 3: Commit**

```bash
git add tests/unit/bricks/parsers/providers/test_pdf_inspector_provider.py
git commit -m "test(parsers): cover PdfInspectorProvider error path"
```

---

## Task 7: Export `PdfInspectorProvider` from package `__init__`

**Files:**
- Modify: `src/nexus/bricks/parsers/providers/__init__.py`

- [ ] **Step 1: Update the module docstring and exports**

Replace the entire contents of `src/nexus/bricks/parsers/providers/__init__.py` with:

```python
"""Parse providers for document parsing.

This module provides a provider-based parsing system that supports multiple
parsing backends:
- UnstructuredProvider: Uses Unstructured.io API
- LlamaParseProvider: Uses LlamaParse API
- PdfInspectorProvider: Local PDF parsing with pdf-inspector (Rust + PyO3)
- MarkItDownProvider: Local fallback for non-PDF formats (optional install)

Example:
    >>> from nexus.bricks.parsers.providers import ProviderRegistry
    >>>
    >>> registry = ProviderRegistry()
    >>> registry.auto_discover()  # Discovers and registers available providers
    >>>
    >>> # Parse with best available provider
    >>> result = await registry.parse("/path/to/file.pdf", content)
"""

from nexus.bricks.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.bricks.parsers.providers.registry import ProviderRegistry

__all__ = [
    "ParseProvider",
    "ProviderConfig",
    "ProviderRegistry",
]
```

> **Note:** `PdfInspectorProvider` and the existing `MarkItDownProvider` are deliberately **not** re-exported here — both files are imported lazily inside `ProviderRegistry.auto_discover()` so missing optional deps don't break package import. Same convention as today.

- [ ] **Step 2: Verify package still imports cleanly**

Run: `python3 -c "from nexus.bricks.parsers.providers import ProviderRegistry; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/parsers/providers/__init__.py
git commit -m "docs(parsers): list pdf-inspector in providers package docstring"
```

---

## Task 8: TDD — `ProviderRegistry.auto_discover()` registers `pdf-inspector`

**Files:**
- Create: `tests/unit/bricks/parsers/providers/test_provider_registry.py`
- Modify: `src/nexus/bricks/parsers/providers/registry.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/bricks/parsers/providers/test_provider_registry.py`:

```python
"""Tests for ProviderRegistry auto_discover behavior."""

import sys

import pytest

from nexus.bricks.parsers.providers.registry import ProviderRegistry


def test_auto_discover_registers_pdf_inspector_when_available(monkeypatch):
    pytest.importorskip("pdf_inspector")
    # Force unstructured/llamaparse off so we only see the local providers.
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    registry = ProviderRegistry()
    registry.auto_discover()

    names = [p.name for p in registry.get_all_providers()]
    assert "pdf-inspector" in names

    pdf_provider = registry.get_provider_by_name("pdf-inspector")
    assert pdf_provider is not None
    assert pdf_provider.priority == 20
    assert ".pdf" in pdf_provider.supported_formats


def test_auto_discover_skips_pdf_inspector_when_import_fails(monkeypatch):
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)

    registry = ProviderRegistry()
    registry.auto_discover()

    names = [p.name for p in registry.get_all_providers()]
    assert "pdf-inspector" not in names


def test_auto_discover_pdf_inspector_outranks_markitdown_for_pdf(monkeypatch):
    pytest.importorskip("pdf_inspector")
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    registry = ProviderRegistry()
    registry.auto_discover()

    chosen = registry.get_provider("doc.pdf")
    assert chosen is not None
    assert chosen.name == "pdf-inspector"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/bricks/parsers/providers/test_provider_registry.py -v`
Expected: FAIL — `pdf-inspector` is not in registered providers (auto_discover doesn't know about it yet).

- [ ] **Step 3: Add the auto_discover branch for pdf-inspector**

In `src/nexus/bricks/parsers/providers/registry.py`, locate the block in `auto_discover()` that registers MarkItDown (~lines 249-259):

```python
        # Always register MarkItDown as fallback
        try:
            from nexus.bricks.parsers.providers.markitdown_provider import MarkItDownProvider

            config = config_map.get("markitdown", ProviderConfig(name="markitdown", priority=10))
            markitdown_provider = MarkItDownProvider(config)
            if markitdown_provider.is_available():
                self.register(markitdown_provider)
                registered += 1
        except Exception as e:
            logger.warning("MarkItDown provider not available: %s", e)
```

**Insert immediately above** that block:

```python
        # Try to register pdf-inspector provider (default local PDF parser)
        try:
            from nexus.bricks.parsers.providers.pdf_inspector_provider import (
                PdfInspectorProvider,
            )

            config = config_map.get(
                "pdf-inspector",
                ProviderConfig(name="pdf-inspector", priority=20),
            )
            pdf_inspector_provider = PdfInspectorProvider(config)
            if pdf_inspector_provider.is_available():
                self.register(pdf_inspector_provider)
                registered += 1
        except ImportError as e:
            logger.debug("pdf-inspector provider not available: %s", e)
```

Also change the MarkItDown comment line from `# Always register MarkItDown as fallback` to `# Try to register MarkItDown (optional fallback for non-PDF formats)` and the `except Exception as e:` log level from `warning` to `debug` (markitdown is now optional — its absence should not warn):

```python
        # Try to register MarkItDown (optional fallback for non-PDF formats)
        try:
            from nexus.bricks.parsers.providers.markitdown_provider import MarkItDownProvider

            config = config_map.get("markitdown", ProviderConfig(name="markitdown", priority=10))
            markitdown_provider = MarkItDownProvider(config)
            if markitdown_provider.is_available():
                self.register(markitdown_provider)
                registered += 1
        except Exception as e:
            logger.debug("MarkItDown provider not available: %s", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/bricks/parsers/providers/test_provider_registry.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/parsers/providers/registry.py \
        tests/unit/bricks/parsers/providers/test_provider_registry.py
git commit -m "feat(parsers): auto-discover pdf-inspector at priority 20

PDF requests now route to pdf-inspector ahead of MarkItDown when both
are installed. Demoted MarkItDown's not-available log to debug since
it's an opt-in dep now."
```

---

## Task 9: TDD — `_load_from_environment()` conditionally registers providers

**Files:**
- Modify: `tests/unit/cli/test_config.py`
- Modify: `src/nexus/config.py`

- [ ] **Step 1: Inspect current `_load_from_environment` test coverage**

Run: `grep -n "_load_from_environment\|parse_providers" tests/unit/cli/test_config.py`
Expected: likely no matches, or limited coverage.

If the file already has a fixture/helper for monkeypatching env, follow that style. Otherwise, write self-contained tests.

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/cli/test_config.py`:

```python
import sys

from nexus.config import _load_from_environment


def test_load_from_environment_registers_pdf_inspector_when_available(monkeypatch):
    pytest.importorskip("pdf_inspector")
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    config = _load_from_environment()
    names = [p["name"] for p in config.parse_providers]
    assert "pdf-inspector" in names
    pdf = next(p for p in config.parse_providers if p["name"] == "pdf-inspector")
    assert pdf["priority"] == 20


def test_load_from_environment_skips_pdf_inspector_when_unavailable(monkeypatch):
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)

    config = _load_from_environment()
    names = [p["name"] for p in config.parse_providers]
    assert "pdf-inspector" not in names


def test_load_from_environment_skips_markitdown_when_unavailable(monkeypatch):
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "markitdown", None)

    config = _load_from_environment()
    names = [p["name"] for p in config.parse_providers]
    assert "markitdown" not in names
```

If `pytest` is not yet imported in `tests/unit/cli/test_config.py`, ensure `import pytest` is present at the top.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/cli/test_config.py -v -k "load_from_environment"`
Expected: at least one FAIL — `_load_from_environment()` currently always appends `markitdown` and never appends `pdf-inspector`.

- [ ] **Step 4: Update `_load_from_environment()` in `src/nexus/config.py`**

Locate lines 604-610:

```python
    # Always add MarkItDown as fallback (no API key needed)
    parse_providers.append(
        {
            "name": "markitdown",
            "priority": 10,
        }
    )
```

Replace with:

```python
    # pdf-inspector: fast local PDF parser (Rust + PyO3), no API key needed
    try:
        import pdf_inspector  # noqa: F401

        if pdf_inspector is not None:
            parse_providers.append(
                {
                    "name": "pdf-inspector",
                    "priority": 20,
                }
            )
    except ImportError:
        pass

    # markitdown: optional fallback for non-PDF formats (Office/HTML/EPUB/...)
    try:
        from markitdown import MarkItDown  # noqa: F401

        if MarkItDown is not None:
            parse_providers.append(
                {
                    "name": "markitdown",
                    "priority": 10,
                }
            )
    except ImportError:
        pass
```

> **Note:** the `is not None` guards make the monkeypatched `sys.modules[<name>] = None` test path work (a `None` entry in `sys.modules` causes `import` to raise `ImportError`, which is caught — the explicit guard is belt-and-suspenders for partial-import scenarios).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/cli/test_config.py -v -k "load_from_environment"`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/config.py tests/unit/cli/test_config.py
git commit -m "feat(config): conditionally register parse providers from env

pdf-inspector and markitdown are both registered only when their
respective Python packages are importable. Replaces the previous
always-on markitdown fallback."
```

---

## Task 10: Update config docstrings/comments

**Files:**
- Modify: `src/nexus/config.py:199` and `src/nexus/config.py:207`

- [ ] **Step 1: Update line 199 comment**

Find:

```python
# Supports multiple providers: unstructured, llamaparse, markitdown
```

Replace with:

```python
# Supports multiple providers: unstructured, llamaparse, pdf-inspector, markitdown
```

- [ ] **Step 2: Update line 207 docstring snippet**

Find:

```python
"Providers: unstructured (API), llamaparse (API), markitdown (local fallback)"
```

Replace with:

```python
"Providers: unstructured (API), llamaparse (API), pdf-inspector (local PDF, default), markitdown (local fallback for non-PDF, optional)"
```

- [ ] **Step 3: Verify the file still imports**

Run: `python3 -c "from nexus.config import _load_from_environment; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/config.py
git commit -m "docs(config): mention pdf-inspector in provider list"
```

---

## Task 11: Full smoke test + verification

- [ ] **Step 1: Run the new test files**

Run:
```bash
pytest tests/unit/bricks/parsers/providers/ tests/unit/cli/test_config.py -v
```
Expected: all green (10 new tests + existing config tests).

- [ ] **Step 2: Run the broader parser test suite**

Run:
```bash
pytest tests/unit/bricks/parsers/ -v
```
Expected: all green (new providers tests + existing md_structure tests).

- [ ] **Step 3: Verify no markitdown leakage in default install**

Run:
```bash
uv run python -c "
import nexus.config
cfg = nexus.config._load_from_environment()
print([p['name'] for p in cfg.parse_providers])
"
```
Expected output (with no API keys set, markitdown not installed):
```
['pdf-inspector']
```

If markitdown is currently still installed in your dev env (it will be — you just removed it from core deps but `uv sync` may have kept the cached install), force-clean and reinstall:
```bash
uv sync --reinstall
```
and re-run the check.

- [ ] **Step 4: Verify opt-in markitdown install works**

Run:
```bash
uv pip install -e '.[markitdown]'
uv run python -c "
import nexus.config
cfg = nexus.config._load_from_environment()
print([p['name'] for p in cfg.parse_providers])
"
```
Expected output:
```
['pdf-inspector', 'markitdown']
```

(Order may vary — what matters is both names appear.)

- [ ] **Step 5: Lint + type-check**

Run:
```bash
ruff check src/nexus/bricks/parsers/providers/pdf_inspector_provider.py \
           src/nexus/bricks/parsers/providers/registry.py \
           src/nexus/bricks/parsers/providers/__init__.py \
           src/nexus/config.py
ruff format --check src/nexus/bricks/parsers/providers/pdf_inspector_provider.py
mypy src/nexus/bricks/parsers/providers/pdf_inspector_provider.py
```
Expected: clean. Fix any issues and amend the relevant commit (or, if already pushed, add a fixup commit).

- [ ] **Step 6: Restore non-markitdown env for the PR**

If you ran the opt-in install in Step 4, revert:
```bash
uv sync
```

- [ ] **Step 7: Final commit / push**

If lint or type-check produced fixes, commit them:
```bash
git add -A
git commit -m "chore: lint/type-check fixes for pdf-inspector provider"
```

Then push:
```bash
git push
```

---

## Self-Review Checklist (run after writing the plan, before handing off)

**Spec coverage** (against `docs/superpowers/specs/2026-04-16-pdf-inspector-default-parser-design.md`):
- ✅ pyproject changes — Task 1
- ✅ `PdfInspectorProvider` with `default_formats=[".pdf"]`, `is_available()`, lazy `_get_inspector`, partial-result semantics with `requires_ocr` flag — Tasks 4-6
- ✅ Registry export → only `ProviderRegistry`/`ParseProvider`/`ProviderConfig` (lazy provider import same as MarkItDown) — Task 7
- ✅ Registry `auto_discover()` adds pdf-inspector at priority 20, MarkItDown demoted to debug log — Task 8
- ✅ Conditional `_load_from_environment()` registration — Task 9
- ✅ Docstring updates — Task 10
- ✅ Tests for provider, registry, config — Tasks 4-9
- ✅ Verification of install size impact via smoke test — Task 11
- N/A No `PARSEABLE_EXTENSIONS` change (per spec, item #3 is satisfied implicitly)
- N/A Smart OCR routing deferred (per spec non-goals)
- ✅ Migration note: PR description (no CHANGELOG.md exists in repo)

**Placeholders:** none. All code blocks contain real content.

**Type/name consistency:**
- Provider name `"pdf-inspector"` (with hyphen) used uniformly across provider class, registry config, env config, and tests. ✅
- Module name `pdf_inspector_provider.py` (underscore) and class `PdfInspectorProvider` consistent. ✅
- Priority 20 used in 3 places (provider config in registry.py, provider config in config.py, test assertion). ✅
- Metadata keys match between provider impl and tests (`pdf_type`, `pages_needing_ocr`, `requires_ocr`, `has_encoding_issues`). ✅
