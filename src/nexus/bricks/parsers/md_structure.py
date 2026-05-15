"""Canonical markdown structure parser — Issue #3718.

Parses markdown into a hierarchical section index with nested blocks,
byte offsets, line numbers, and token estimates.

Uses ``markdown-it-pyrs`` (Rust, ~10x faster) when available, with
automatic fallback to ``markdown-it-py`` (pure Python, CommonMark-
compliant).

This module is the **single source of truth** for markdown structure
parsing.  ``extract_structure()`` and ``create_chunks()`` in
``parsers/utils.py`` delegate to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

# Schema version — bump when the stored JSON shape changes.
# v2: added paragraph, blockquote, list, heading block types (#3720).
SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BlockInfo:
    """A notable block (code fence, table) inside a section."""

    type: str  # "code", "table"
    byte_start: int
    byte_end: int
    line_start: int  # 0-indexed
    line_end: int  # 0-indexed, exclusive
    language: str | None = None  # code blocks only
    rows: int | None = None  # tables only


@dataclass(slots=True)
class SectionInfo:
    """A heading-delimited section of a markdown document."""

    heading: str
    depth: int  # 1–6
    byte_start: int
    byte_end: int
    line_start: int  # 0-indexed
    line_end: int  # 0-indexed, exclusive
    tokens_est: int = 0
    blocks: list[BlockInfo] = field(default_factory=list)


@dataclass(slots=True)
class FrontmatterInfo:
    """YAML frontmatter block."""

    byte_start: int
    byte_end: int
    line_start: int
    line_end: int
    keys: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MarkdownStructureIndex:
    """Full structural index for a markdown document."""

    version: int = SCHEMA_VERSION
    content_id: str = ""
    tokens_est_method: str = "bytes/4"
    frontmatter: FrontmatterInfo | None = None
    sections: list[SectionInfo] = field(default_factory=list)

    # ── Serialization ─────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "version": self.version,
            "content_id": self.content_id,
            "tokens_est_method": self.tokens_est_method,
            "sections": [_section_to_dict(s) for s in self.sections],
        }
        if self.frontmatter is not None:
            d["frontmatter"] = _frontmatter_to_dict(self.frontmatter)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarkdownStructureIndex:
        fm_data = data.get("frontmatter")
        fm = _frontmatter_from_dict(fm_data) if fm_data else None
        return cls(
            version=data.get("version", SCHEMA_VERSION),
            content_id=data.get("content_id", ""),
            tokens_est_method=data.get("tokens_est_method", "bytes/4"),
            frontmatter=fm,
            sections=[_section_from_dict(s) for s in data.get("sections", [])],
        )


def _section_to_dict(s: SectionInfo) -> dict[str, Any]:
    d: dict[str, Any] = {
        "heading": s.heading,
        "depth": s.depth,
        "byte_start": s.byte_start,
        "byte_end": s.byte_end,
        "line_start": s.line_start,
        "line_end": s.line_end,
        "tokens_est": s.tokens_est,
        "blocks": [_block_to_dict(b) for b in s.blocks],
    }
    return d


def _block_to_dict(b: BlockInfo) -> dict[str, Any]:
    d: dict[str, Any] = {
        "type": b.type,
        "byte_start": b.byte_start,
        "byte_end": b.byte_end,
        "line_start": b.line_start,
        "line_end": b.line_end,
    }
    if b.language is not None:
        d["language"] = b.language
    if b.rows is not None:
        d["rows"] = b.rows
    return d


def _frontmatter_to_dict(fm: FrontmatterInfo) -> dict[str, Any]:
    return {
        "byte_start": fm.byte_start,
        "byte_end": fm.byte_end,
        "line_start": fm.line_start,
        "line_end": fm.line_end,
        "keys": fm.keys,
    }


def _section_from_dict(data: dict[str, Any]) -> SectionInfo:
    return SectionInfo(
        heading=data["heading"],
        depth=data["depth"],
        byte_start=data["byte_start"],
        byte_end=data["byte_end"],
        line_start=data["line_start"],
        line_end=data["line_end"],
        tokens_est=data.get("tokens_est", 0),
        blocks=[_block_from_dict(b) for b in data.get("blocks", [])],
    )


def _block_from_dict(data: dict[str, Any]) -> BlockInfo:
    return BlockInfo(
        type=data["type"],
        byte_start=data["byte_start"],
        byte_end=data["byte_end"],
        line_start=data["line_start"],
        line_end=data["line_end"],
        language=data.get("language"),
        rows=data.get("rows"),
    )


def _frontmatter_from_dict(data: dict[str, Any]) -> FrontmatterInfo:
    return FrontmatterInfo(
        byte_start=data["byte_start"],
        byte_end=data["byte_end"],
        line_start=data["line_start"],
        line_end=data["line_end"],
        keys=data.get("keys", []),
    )


# ---------------------------------------------------------------------------
# Parser — Rust fast path (markdown-it-pyrs) with Python fallback
# ---------------------------------------------------------------------------

_USE_RUST: bool | None = None
_md_rust: Any = None
_md_py: MarkdownIt | None = None


def _init_rust_parser() -> bool:
    """Try to initialise the Rust-backed parser.  Returns True on success."""
    global _md_rust, _USE_RUST  # noqa: PLW0603
    try:
        from markdown_it_pyrs import MarkdownIt as MdRs

        _md_rust = MdRs()
        _md_rust.enable("table")
        _md_rust.enable("front_matter")
        _USE_RUST = True
        logger.debug("markdown-it-pyrs (Rust) available — using fast path")
        return True
    except (ImportError, Exception):
        _USE_RUST = False
        logger.debug("markdown-it-pyrs not available — falling back to Python parser")
        return False


def _get_python_parser() -> MarkdownIt:
    """Lazily initialise the Python ``MarkdownIt`` fallback."""
    global _md_py  # noqa: PLW0603
    if _md_py is None:
        _md_py = MarkdownIt("commonmark").enable("table")
        try:
            from mdit_py_plugins.front_matter import front_matter_plugin

            front_matter_plugin(_md_py)
        except ImportError:
            logger.debug("mdit_py_plugins not installed — frontmatter parsing disabled")
    return _md_py


def _build_line_byte_offsets(content: bytes) -> list[int]:
    """Return a list where ``offsets[i]`` is the byte offset of line *i*.

    An extra sentinel entry is appended equal to ``len(content)`` so that
    the byte range for line *i* is ``[offsets[i], offsets[i + 1])``.
    """
    offsets: list[int] = [0]
    pos = 0
    while pos < len(content):
        nl = content.find(b"\n", pos)
        if nl == -1:
            break
        offsets.append(nl + 1)
        pos = nl + 1
    offsets.append(len(content))
    return offsets


def parse_markdown_structure(
    content: bytes,
    content_id: str = "",
) -> MarkdownStructureIndex:
    """Parse markdown *content* (UTF-8 bytes) into a structural index.

    Uses ``markdown-it-pyrs`` (Rust, ~10x faster) when available, with
    automatic fallback to ``markdown-it-py`` (pure Python).

    Args:
        content: Raw UTF-8 bytes of the markdown file.
        content_id: Etag / content hash to embed in the index for
            staleness detection on the read path.

    Returns:
        A ``MarkdownStructureIndex`` ready for JSON serialization and
        storage in ``FileMetadataModel``.
    """
    global _USE_RUST  # noqa: PLW0603
    if _USE_RUST is None:
        _init_rust_parser()

    if _USE_RUST:
        return _parse_rust(content, content_id)
    return _parse_python(content, content_id)


# ---------------------------------------------------------------------------
# Rust fast path — markdown-it-pyrs Node tree (byte offsets are native)
# ---------------------------------------------------------------------------


def _parse_rust(content: bytes, content_id: str) -> MarkdownStructureIndex:
    text = content.decode("utf-8", errors="replace")
    tree = _md_rust.tree(text)
    line_offsets = _build_line_byte_offsets(content)

    headings: list[_RawHeading] = []
    blocks: list[BlockInfo] = []
    frontmatter: FrontmatterInfo | None = None

    def _walk(node: Any) -> None:
        """Recursively walk the AST to find blocks nested inside containers."""
        nonlocal frontmatter
        srcmap = node.srcmap
        if not srcmap:
            for child in node.children:
                _walk(child)
            return

        byte_start, byte_end = srcmap

        # Helper: exclusive line_end for Rust srcmap byte ranges.
        def _excl_line_end() -> int:
            return _byte_end_to_line_exclusive(byte_end, byte_start, line_offsets)

        if node.name == "front_matter":
            fm_text = content[byte_start:byte_end].decode("utf-8", errors="replace")
            keys = _extract_yaml_keys(fm_text)
            frontmatter = FrontmatterInfo(
                byte_start=byte_start,
                byte_end=byte_end,
                line_start=_byte_to_line(byte_start, line_offsets),
                line_end=_excl_line_end(),
                keys=keys,
            )

        elif node.name in ("heading", "lheading"):
            raw = content[byte_start:byte_end].decode("utf-8", errors="replace")
            heading_text = ""
            depth = 1
            if raw.startswith("#"):
                depth = len(raw) - len(raw.lstrip("#"))
                heading_text = raw.lstrip("#").strip().split("\n")[0]
            elif node.name == "lheading":
                lines = raw.strip().split("\n")
                heading_text = lines[0].strip() if lines else ""
                depth = 1 if len(lines) > 1 and lines[-1].startswith("=") else 2
            else:
                for child in node.children:
                    if child.name == "text" and child.srcmap:
                        cs, ce = child.srcmap
                        heading_text = content[cs:ce].decode("utf-8", errors="replace").strip()
                        break
            h_line_start = _byte_to_line(byte_start, line_offsets)
            headings.append(
                _RawHeading(
                    text=heading_text,
                    depth=depth,
                    line_start=h_line_start,
                )
            )
            # Issue #3720: also record as a searchable block.
            blocks.append(
                BlockInfo(
                    type="heading",
                    byte_start=byte_start,
                    byte_end=byte_end,
                    line_start=h_line_start,
                    line_end=_excl_line_end(),
                )
            )

        elif node.name == "fence":
            language = node.attrs.get("language") if node.attrs else None
            if not language:
                first_line = (
                    content[byte_start:byte_end].decode("utf-8", errors="replace").split("\n")[0]
                )
                lang_part = first_line.lstrip("`").strip()
                language = lang_part or None
            blocks.append(
                BlockInfo(
                    type="code",
                    byte_start=byte_start,
                    byte_end=byte_end,
                    line_start=_byte_to_line(byte_start, line_offsets),
                    line_end=_excl_line_end(),
                    language=language,
                )
            )

        elif node.name == "table":
            row_count = 0
            for child in node.children:
                if child.name == "tbody":
                    row_count = sum(1 for gc in child.children if gc.name == "trow")
            blocks.append(
                BlockInfo(
                    type="table",
                    byte_start=byte_start,
                    byte_end=byte_end,
                    line_start=_byte_to_line(byte_start, line_offsets),
                    line_end=_excl_line_end(),
                    rows=row_count,
                )
            )

        # Issue #3720: track paragraph, blockquote, list blocks.
        elif node.name == "paragraph":
            blocks.append(
                BlockInfo(
                    type="paragraph",
                    byte_start=byte_start,
                    byte_end=byte_end,
                    line_start=_byte_to_line(byte_start, line_offsets),
                    line_end=_excl_line_end(),
                )
            )

        elif node.name == "blockquote":
            blocks.append(
                BlockInfo(
                    type="blockquote",
                    byte_start=byte_start,
                    byte_end=byte_end,
                    line_start=_byte_to_line(byte_start, line_offsets),
                    line_end=_excl_line_end(),
                )
            )

        elif node.name in ("bullet_list", "ordered_list"):
            blocks.append(
                BlockInfo(
                    type="list",
                    byte_start=byte_start,
                    byte_end=byte_end,
                    line_start=_byte_to_line(byte_start, line_offsets),
                    line_end=_excl_line_end(),
                )
            )

        # Recurse into container blocks (blockquotes, list items, etc.)
        # to find nested fences/tables and other tracked blocks.
        if node.name not in ("fence", "front_matter", "heading", "lheading", "table", "paragraph"):
            for child in node.children:
                _walk(child)

    _walk(tree)
    return _build_index(headings, blocks, frontmatter, line_offsets, content_id)


def _byte_to_line(byte_offset: int, line_offsets: list[int]) -> int:
    """Convert a byte offset to a 0-indexed line number (binary search)."""
    lo, hi = 0, len(line_offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_offsets[mid] <= byte_offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _byte_end_to_line_exclusive(byte_end: int, byte_start: int, line_offsets: list[int]) -> int:
    """Convert an *exclusive* byte-end offset to an exclusive 0-indexed line.

    Rust's ``srcmap`` gives ``(byte_start, byte_end)`` where ``byte_end``
    is exclusive (one past the last byte).  ``_byte_to_line(byte_end)``
    returns the line *containing* ``byte_end``, which equals
    ``line_start`` for single-line blocks — producing an empty
    ``[start, start)`` range.

    Fix: convert the last *inclusive* byte (``byte_end - 1``) to a line,
    then add 1 to make the result exclusive.
    """
    if byte_end <= byte_start:
        return _byte_to_line(byte_start, line_offsets) + 1
    return _byte_to_line(byte_end - 1, line_offsets) + 1


# ---------------------------------------------------------------------------
# Python fallback — markdown-it-py token stream (line numbers → byte offsets)
# ---------------------------------------------------------------------------


def _parse_python(content: bytes, content_id: str) -> MarkdownStructureIndex:
    text = content.decode("utf-8", errors="replace")
    md = _get_python_parser()
    tokens = md.parse(text)
    line_offsets = _build_line_byte_offsets(content)

    headings: list[_RawHeading] = []
    blocks: list[BlockInfo] = []
    frontmatter: FrontmatterInfo | None = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "front_matter" and tok.map is not None:
            fm_line_start, fm_line_end = tok.map
            fm_byte_start = line_offsets[fm_line_start]
            fm_byte_end = line_offsets[min(fm_line_end, len(line_offsets) - 1)]
            keys = _extract_yaml_keys(tok.content)
            frontmatter = FrontmatterInfo(
                byte_start=fm_byte_start,
                byte_end=fm_byte_end,
                line_start=fm_line_start,
                line_end=fm_line_end,
                keys=keys,
            )

        elif tok.type == "heading_open" and tok.map is not None:
            depth = int(tok.tag[1])
            heading_text = ""
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                heading_text = tokens[i + 1].content
            h_start, h_end = tok.map
            headings.append(
                _RawHeading(
                    text=heading_text,
                    depth=depth,
                    line_start=h_start,
                )
            )
            # Issue #3720: also record as a searchable block.
            blocks.append(
                BlockInfo(
                    type="heading",
                    byte_start=line_offsets[h_start],
                    byte_end=line_offsets[min(h_end, len(line_offsets) - 1)],
                    line_start=h_start,
                    line_end=h_end,
                )
            )

        elif tok.type == "fence" and tok.map is not None:
            bl_start, bl_end = tok.map
            blocks.append(
                BlockInfo(
                    type="code",
                    byte_start=line_offsets[bl_start],
                    byte_end=line_offsets[min(bl_end, len(line_offsets) - 1)],
                    line_start=bl_start,
                    line_end=bl_end,
                    language=tok.info.strip() or None,
                )
            )

        elif tok.type == "table_open" and tok.map is not None:
            tbl_start, tbl_end = tok.map
            row_count = 0
            j = i + 1
            in_tbody = False
            while j < len(tokens) and tokens[j].type != "table_close":
                if tokens[j].type == "tbody_open":
                    in_tbody = True
                elif tokens[j].type == "tbody_close":
                    in_tbody = False
                elif tokens[j].type == "tr_open" and in_tbody:
                    row_count += 1
                j += 1
            blocks.append(
                BlockInfo(
                    type="table",
                    byte_start=line_offsets[tbl_start],
                    byte_end=line_offsets[min(tbl_end, len(line_offsets) - 1)],
                    line_start=tbl_start,
                    line_end=tbl_end,
                    rows=row_count,
                )
            )

        # Issue #3720: paragraph, blockquote, list blocks.
        elif tok.type == "paragraph_open" and tok.map is not None:
            p_start, p_end = tok.map
            blocks.append(
                BlockInfo(
                    type="paragraph",
                    byte_start=line_offsets[p_start],
                    byte_end=line_offsets[min(p_end, len(line_offsets) - 1)],
                    line_start=p_start,
                    line_end=p_end,
                )
            )

        elif tok.type == "blockquote_open" and tok.map is not None:
            bq_start, bq_end = tok.map
            blocks.append(
                BlockInfo(
                    type="blockquote",
                    byte_start=line_offsets[bq_start],
                    byte_end=line_offsets[min(bq_end, len(line_offsets) - 1)],
                    line_start=bq_start,
                    line_end=bq_end,
                )
            )

        elif tok.type in ("bullet_list_open", "ordered_list_open") and tok.map is not None:
            li_start, li_end = tok.map
            blocks.append(
                BlockInfo(
                    type="list",
                    byte_start=line_offsets[li_start],
                    byte_end=line_offsets[min(li_end, len(line_offsets) - 1)],
                    line_start=li_start,
                    line_end=li_end,
                )
            )

        i += 1

    return _build_index(headings, blocks, frontmatter, line_offsets, content_id)


# ---------------------------------------------------------------------------
# Shared section builder
# ---------------------------------------------------------------------------


def _build_index(
    headings: list[_RawHeading],
    blocks: list[BlockInfo],
    frontmatter: FrontmatterInfo | None,
    line_offsets: list[int],
    content_id: str,
) -> MarkdownStructureIndex:
    """Build hierarchical sections from raw headings and blocks."""
    total_lines = len(line_offsets) - 1
    sections: list[SectionInfo] = []

    # Issue #3720 (Codex R1): synthesize a root section for content
    # before the first heading (or the entire file if headingless) so
    # blocks in that region are searchable via block_type filtering.
    first_heading_line = headings[0].line_start if headings else total_lines
    fm_end = frontmatter.line_end if frontmatter else 0
    preamble_start = fm_end  # skip frontmatter lines
    if first_heading_line > preamble_start:
        pre_blocks = [
            b for b in blocks if b.line_start >= preamble_start and b.line_end <= first_heading_line
        ]
        if pre_blocks:
            pre_byte_start = line_offsets[preamble_start]
            pre_byte_end = line_offsets[min(first_heading_line, len(line_offsets) - 1)]
            sections.append(
                SectionInfo(
                    heading="",
                    depth=0,
                    byte_start=pre_byte_start,
                    byte_end=pre_byte_end,
                    line_start=preamble_start,
                    line_end=first_heading_line,
                    tokens_est=(pre_byte_end - pre_byte_start) // 4,
                    blocks=pre_blocks,
                )
            )

    for idx, h in enumerate(headings):
        section_line_start = h.line_start
        section_line_end = total_lines
        for nxt in headings[idx + 1 :]:
            if nxt.depth <= h.depth:
                section_line_end = nxt.line_start
                break

        sec_byte_start = line_offsets[section_line_start]
        sec_byte_end = line_offsets[min(section_line_end, len(line_offsets) - 1)]
        sec_tokens_est = (sec_byte_end - sec_byte_start) // 4

        sec_blocks = [
            b
            for b in blocks
            if b.line_start >= section_line_start and b.line_end <= section_line_end
        ]

        sections.append(
            SectionInfo(
                heading=h.text,
                depth=h.depth,
                byte_start=sec_byte_start,
                byte_end=sec_byte_end,
                line_start=section_line_start,
                line_end=section_line_end,
                tokens_est=sec_tokens_est,
                blocks=sec_blocks,
            )
        )

    return MarkdownStructureIndex(
        version=SCHEMA_VERSION,
        content_id=content_id,
        frontmatter=frontmatter,
        sections=sections,
    )


# ---------------------------------------------------------------------------
# Section lookup helpers (used by read path)
# ---------------------------------------------------------------------------


def find_section(
    index: MarkdownStructureIndex,
    section_name: str,
) -> SectionInfo | None:
    """Find a section by heading text (case-insensitive substring match).

    Exact match is tried first; falls back to substring.
    """
    lower = section_name.lower()
    # Exact match first
    for s in index.sections:
        if s.heading.lower() == lower:
            return s
    # Substring fallback
    for s in index.sections:
        if lower in s.heading.lower():
            return s
    return None


def filter_blocks(
    section: SectionInfo,
    block_type: str,
) -> list[BlockInfo]:
    """Return blocks within *section* matching *block_type*."""
    return [b for b in section.blocks if b.type == block_type]


def slice_content(
    content: bytes,
    byte_start: int,
    byte_end: int,
) -> str:
    """Decode a byte range to a UTF-8 string."""
    return content[byte_start:byte_end].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RawHeading:
    """Intermediate heading data before section boundaries are computed."""

    text: str
    depth: int
    line_start: int


def _extract_yaml_keys(frontmatter_content: str) -> list[str]:
    """Extract top-level YAML keys from frontmatter content.

    Uses a simple line-based approach rather than a full YAML parser to
    avoid adding a dependency and to handle malformed YAML gracefully.
    """
    keys: list[str] = []
    for line in frontmatter_content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped and not stripped.startswith(" ") and not stripped.startswith("\t"):
            key = stripped.split(":", 1)[0].strip()
            if key and key != "---":
                keys.append(key)
    return keys
