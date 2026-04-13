"""MarkdownStructureWriteHook — synchronous post-write indexing for .md files.

Issue #3718: On every ``.md`` write, parse the markdown structure and store
the index as ``md_structure`` metadata.  The index enables partial reads
by section/block without loading the full file.

Architecture decisions:
    - **Synchronous** (not background) because markdown-it-py parses 50 KB
      in < 1 ms — no reason to complicate with threading.
    - **Lazy hash validation** on the read path: if the stored
      ``content_hash`` doesn't match the file's current etag, re-parse
      inline and update the index (self-healing).
    - Follows the ``AutoParseWriteHook`` pattern for DI and registration.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.bricks.parsers.md_structure import (
    MarkdownStructureIndex,
    filter_blocks,
    find_section,
    parse_markdown_structure,
    slice_content,
)
from nexus.contracts.vfs_hooks import WriteHookContext

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec

logger = logging.getLogger(__name__)

# Metadata key used to store the structural index.
MD_STRUCTURE_KEY = "md_structure"


class MarkdownStructureWriteHook:
    """Post-write hook that builds a structural index for markdown files.

    Dependencies injected at construction:
        metadata: MetastoreABC — for storing/retrieving the index.
    """

    def __init__(self, metadata: Any = None) -> None:
        self._metadata = metadata

    # ── Hook spec (duck-typed, matches AutoParseWriteHook pattern) ──

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(write_hooks=(self,))

    @property
    def name(self) -> str:
        return "md_structure"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        """Parse markdown structure and store index after .md writes."""
        if self._metadata is None:
            return
        if not ctx.path.endswith(".md"):
            return

        try:
            content_hash = ctx.content_hash or ""
            index = parse_markdown_structure(ctx.content, content_hash=content_hash)
            self._metadata.set_file_metadata(
                ctx.path,
                MD_STRUCTURE_KEY,
                json.dumps(index.to_dict()),
            )
        except Exception:
            logger.debug("md_structure indexing failed for %s", ctx.path, exc_info=True)

    # ── Read-path helpers ────────────────────────────────────────

    def get_index(
        self,
        path: str,
        current_content: bytes | None = None,
        current_hash: str | None = None,
    ) -> MarkdownStructureIndex | None:
        """Retrieve the structural index for *path*, re-parsing if stale.

        Args:
            path: Virtual file path.
            current_content: If provided, used for stale-index re-parse
                instead of requiring a separate read.
            current_hash: Current etag of the file — compared against the
                stored ``content_hash`` to detect staleness.

        Returns:
            The index, or ``None`` if no index exists and no content was
            provided for on-demand parsing.
        """
        if self._metadata is None:
            return None

        # If the caller can't provide a hash (e.g. connector paths with no
        # authoritative etag), skip the cache entirely and parse from content.
        # This prevents stale cached indices from serving wrong byte ranges.
        if not current_hash and current_content is not None:
            return self._reindex(path, current_content, "")

        raw = self._metadata.get_file_metadata(path, MD_STRUCTURE_KEY)
        if raw is not None:
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                index = MarkdownStructureIndex.from_dict(data)

                # Lazy hash validation
                if current_hash and index.content_hash and index.content_hash != current_hash:
                    logger.debug("Stale md_structure for %s — re-parsing", path)
                    if current_content is not None:
                        return self._reindex(path, current_content, current_hash)
                    # No content provided — return stale index (caller
                    # doesn't have content, so we can't re-parse).
                    return index
                return index
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.debug("Corrupt md_structure for %s — discarding", path, exc_info=True)

        # No stored index — parse on demand if content available.
        if current_content is not None:
            return self._reindex(path, current_content, current_hash or "")
        return None

    def read_section(
        self,
        path: str,
        content: bytes,
        content_hash: str,
        section: str,
        block_type: str | None = None,
    ) -> str | None:
        """Read a specific section (optionally filtered by block type).

        Special section values:
            ``"*"`` — return the structure listing as JSON (no content).
            ``"frontmatter"`` — return the raw frontmatter block.

        Returns the section content as a string, or ``None`` if the
        section wasn't found (caller should fall back to full content).
        """
        index = self.get_index(path, current_content=content, current_hash=content_hash)
        if index is None:
            return None

        # Special: structure listing
        if section == "*":
            listing = self.get_structure_listing(path, content=content, content_hash=content_hash)
            return json.dumps(listing, indent=2) if listing is not None else None

        # Special: frontmatter
        if section.lower() == "frontmatter":
            if index.frontmatter is None:
                return None
            return slice_content(
                content,
                index.frontmatter.byte_start,
                index.frontmatter.byte_end,
            )

        sec = find_section(index, section)
        if sec is None:
            return None

        if block_type:
            blocks = filter_blocks(sec, block_type)
            if not blocks:
                return None
            # Concatenate all matching blocks within the section.
            parts = [slice_content(content, b.byte_start, b.byte_end) for b in blocks]
            return "\n\n".join(parts)

        return slice_content(content, sec.byte_start, sec.byte_end)

    def get_structure_listing(
        self,
        path: str,
        content: bytes | None = None,
        content_hash: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Return a lightweight structure listing (no content).

        Used by the ``nexus_md_structure`` MCP tool and REST endpoint.
        """
        index = self.get_index(path, current_content=content, current_hash=content_hash)
        if index is None:
            return None

        listing: list[dict[str, Any]] = []

        if index.frontmatter:
            listing.append(
                {
                    "type": "frontmatter",
                    "keys": index.frontmatter.keys,
                    "line_start": index.frontmatter.line_start,
                    "line_end": index.frontmatter.line_end,
                }
            )

        for sec in index.sections:
            block_types = list({b.type for b in sec.blocks})
            entry: dict[str, Any] = {
                "type": "section",
                "heading": sec.heading,
                "depth": sec.depth,
                "tokens_est": sec.tokens_est,
                "line_start": sec.line_start,
                "line_end": sec.line_end,
                "blocks": block_types,
            }
            listing.append(entry)

        return listing

    # ── Internal ─────────────────────────────────────────────────

    def _reindex(
        self,
        _path: str,
        content: bytes,
        content_hash: str,
    ) -> MarkdownStructureIndex:
        """Re-parse on demand (in-memory only — never persisted from read path).

        Only the write hook (``on_post_write``) persists the index, because it
        has an atomically-consistent content/hash pair.  Read-side rebuilds
        cannot guarantee the caller-supplied hash matches the content bytes
        (a write between the read and the hash fetch would create a mismatch),
        so we return an ephemeral index without writing to the metastore.
        """
        return parse_markdown_structure(content, content_hash=content_hash)
