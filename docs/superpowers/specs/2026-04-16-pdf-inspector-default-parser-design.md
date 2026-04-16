# pdf-inspector as default PDF parser, markitdown optional

- **Issue:** [#3757](https://github.com/nexi-lab/nexus/issues/3757)
- **Date:** 2026-04-16
- **Status:** Design approved

## Summary

Move `markitdown[all]` from core to optional dependencies. Add `pdf-inspector` (Rust + PyO3) as the default local PDF provider. Configuration remains graceful: if neither library is installed, the provider registry simply omits the entry. `auto_parse` behavior is not changed by default — existing extension detection keeps working, and the provider registry picks whichever provider is installed.

**Scope:** issue items 1, 2, 3, 4, 6. Item 3 (scoping `auto_parse` to PDF by default) is achieved implicitly: with markitdown no longer in core deps, the provider registry has no handler for Office/HTML formats unless the user opts into `nexus[markitdown]`. No explicit `PARSEABLE_EXTENSIONS` change is introduced. Defers item 5 (smart OCR routing).

## Motivation

`markitdown[all]` pulls ~200MB (pandas, numpy, onnxruntime, lxml, pdfminer, python-pptx, mammoth, openpyxl, etc.) into every `pip install nexus`, even for users who never call the parser. Its PDF parsing also has known gaps: no OCR, no table detection (0.00 TEDS), no heading detection (0.00 MHS), and 0.589 overall accuracy on opendataloader-bench.

`pdf-inspector` is a compact native extension (~2-3MB compiled, zero Python deps) that scores 0.78 overall, adds real table/heading detection, emits Markdown directly, and exposes `pages_needing_ocr` — a list of page indices that lack extractable text. That metadata is the hook for a future smart-OCR routing feature but is not required to ship this change.

## Non-goals

- Smart OCR routing (issue item #5) — provider only surfaces `pages_needing_ocr` in metadata; no cross-provider orchestration is introduced here.
- Explicit changes to `PARSEABLE_EXTENSIONS` or `auto_parse` defaults — item #3 is satisfied implicitly by removing markitdown from core deps and letting `is_available()` gate non-PDF formats.
- Removing `MarkItDownProvider` / `MarkItDownParser` code — both stay in the tree as opt-in providers for Office/HTML/EPUB formats.
- Adding a brick-level `PdfInspectorParser` — only the provider layer is added. The existing `ParserBrick` dispatches to providers via the registry.

## Architecture

### Dependency layout (`pyproject.toml`)

- **Core deps:** remove `markitdown[all]>=0.1.0; python_version < '3.14'`; add `pdf-inspector>=0.1.1; python_version == '3.12'`.
- **Optional deps:** new `[project.optional-dependencies]` group `markitdown = ["markitdown[all]>=0.1.0; python_version < '3.14'"]`.
- **Python version guard rationale:** pdf-inspector 0.1.1 currently ships cp312 wheels only (macOS x86_64, macOS arm64, manylinux x86_64, manylinux aarch64, win_amd64). Nexus already requires `>=3.12`. The guard can be loosened once upstream ships cp313/cp314 wheels.

### New provider (`src/nexus/bricks/parsers/providers/pdf_inspector_provider.py`)

Modeled on `markitdown_provider.py`. One new class:

```python
class PdfInspectorProvider(ParseProvider):
    DEFAULT_FORMATS = [".pdf"]  # PDF-only — no fallthrough

    @property
    def name(self) -> str: return "pdf-inspector"

    @property
    def default_formats(self) -> list[str]: return self.DEFAULT_FORMATS.copy()

    def is_available(self) -> bool:
        try:
            import pdf_inspector  # noqa: F401
            return True
        except Exception:
            return False

    def _get_inspector(self) -> Any:  # thread-safe lazy singleton
        ...

    async def parse(self, content, file_path, metadata=None) -> ParseResult:
        ...
```

**Parse flow:**

1. Run `pdf_inspector.process_pdf_bytes(content)` in a thread executor (sync lib).
2. Treat `result.markdown` as the extracted text.
3. Build chunks with `create_chunks`, structure with `extract_structure` (same utils as MarkItDownProvider).
4. Wrap exceptions as `ParserError(path=file_path, parser=self.name)`.

**Returned `ParseResult.metadata`:**

| Key | Source | Purpose |
|---|---|---|
| `parser` | `"pdf-inspector"` | identifier |
| `format` | `".pdf"` | extension |
| `original_path` | arg | provenance |
| `pdf_type` | `result.pdf_type` | `text_based` / `scanned` / `image_based` / `mixed` |
| `pages_needing_ocr` | `result.pages_needing_ocr` | list[int], 0-indexed |
| `requires_ocr` | `bool(pages_needing_ocr)` | downstream routing flag |
| `has_encoding_issues` | `result.has_encoding_issues` | parser health |

**Partial-result semantics:** pages needing OCR produce no text in `result.markdown`, but the call still succeeds. The provider returns the partial result as-is (no fallback, no raise). `requires_ocr: True` in metadata is the hook a future smart-routing layer will consume. This is option C from the brainstorm.

### Registry wiring (`src/nexus/bricks/parsers/providers/__init__.py`, `registry.py`)

- Export `PdfInspectorProvider` from `providers/__init__.py`.
- Register it alongside `MarkItDownProvider` / `UnstructuredProvider` / `LlamaParseProvider` in `registry.py` (same pattern — instantiated from `ProviderConfig` entries).

### Config wiring (`src/nexus/config.py::_load_from_environment`)

Replace the unconditional MarkItDown-as-fallback block (~lines 604-610) with:

```python
# pdf-inspector: fast Rust PDF parser, no API key needed
try:
    import pdf_inspector  # noqa: F401
    parse_providers.append({"name": "pdf-inspector", "priority": 20})
except ImportError:
    pass

# markitdown: optional fallback for non-PDF formats
try:
    from markitdown import MarkItDown  # noqa: F401
    parse_providers.append({"name": "markitdown", "priority": 10})
except ImportError:
    pass
```

Update the module-level docstring/comments at lines 199 and 207 to list `pdf-inspector` and note that `markitdown` is optional.

### Effective priority order

| Priority | Provider | Trigger |
|---|---|---|
| 100 | unstructured | `UNSTRUCTURED_API_KEY` set |
| 90 | llamaparse | `LLAMA_CLOUD_API_KEY` set |
| 20 | pdf-inspector | library importable (covers `.pdf`) |
| 10 | markitdown | library importable (covers Office/HTML/EPUB/etc.) |

When both local providers are installed, `.pdf` routes to pdf-inspector (higher priority). Non-PDF formats fall through to markitdown if installed; otherwise the registry has no provider for that extension and the parser errors out with the existing `no provider for format` message.

## Testing

### New provider tests (`tests/parsers/providers/test_pdf_inspector_provider.py`)

- `test_is_available_when_installed` — monkeypatch-free: skip if not importable; assert `True` when it is.
- `test_default_formats` — returns `[".pdf"]`.
- `test_parse_text_pdf` — parse a real text-based PDF fixture; assert non-empty markdown, `pdf_type == "text_based"`, `pages_needing_ocr == []`, `requires_ocr is False`.
- `test_parse_scanned_pdf` — parse a scanned fixture; assert `requires_ocr is True`, `pages_needing_ocr` non-empty, markdown may be partial but parse does not raise.
- `test_parse_invalid_bytes` — malformed input raises `ParserError` with `parser="pdf-inspector"`.

**Fixtures:** reuse existing PDFs under `tests/parsers/fixtures/` if present. If no scanned fixture exists, generate a minimal one (single image-only page) or mark the scanned-PDF test with `pytest.importorskip` on the fixture.

### Registry test

Extend `tests/parsers/test_registry.py` (or equivalent) to verify:
- `pdf-inspector` appears in registered providers when importable.
- `pdf-inspector` absent when import fails (simulate via monkeypatching `sys.modules["pdf_inspector"] = None`).

### Config test

Verify `_load_from_environment()` emits `pdf-inspector` entry iff `pdf_inspector` is importable, and `markitdown` entry iff `markitdown` is importable. Use monkeypatching on `sys.modules` to simulate both paths.

## Migration

- **Changelog / release notes:** "PDF parsing now uses `pdf-inspector` by default (~2-3MB native extension). `markitdown` is now optional — install with `pip install nexus[markitdown]` for Office/HTML/EPUB auto-parsing."
- **No code removal.** `MarkItDownProvider` and `MarkItDownParser` stay as opt-in providers.
- **Installation failure modes:**
  - Python 3.13+ user on `pip install nexus` → pdf-inspector version guard skips it; local PDF parsing unavailable until upstream ships wheels (or until Nexus allows the version guard to relax). Cloud providers still work if configured.
  - User needs Office auto-parse → `pip install nexus[markitdown]` restores previous behavior.

## Files touched

| File | Change |
|---|---|
| `pyproject.toml` | move markitdown → optional, add pdf-inspector to core |
| `src/nexus/bricks/parsers/providers/pdf_inspector_provider.py` | new file |
| `src/nexus/bricks/parsers/providers/__init__.py` | export new provider |
| `src/nexus/bricks/parsers/providers/registry.py` | register new provider |
| `src/nexus/config.py` | conditional provider registration; docstring updates |
| `tests/parsers/providers/test_pdf_inspector_provider.py` | new file |
| `tests/parsers/test_registry.py` (or equivalent) | extend |
| `CHANGELOG.md` (or release notes) | migration note |

## Open questions

None. All design choices finalized during brainstorm:

- Scope: option A (core swap, no smart OCR routing).
- Behavior when markitdown not installed: option B (graceful `is_available()` degradation, no hard config change).
- Behavior when PDF has scanned pages: option C (partial result + `requires_ocr: True` metadata flag).
