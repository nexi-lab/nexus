"""Read-side macro-chunk (neighbor-context) expansion for hybrid search (Issue #4398).

Pure and backend-agnostic. Given ranked results and a NeighborFetcher that returns
chunk rows for (path, chunk_index range) spans, expand each hit into its surrounding
section (bounded by heading_prefix, file edge, and a token budget) and attach the
stitched text as ``macro_text``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

_CODE_EXTENSIONS = (
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".py",
    ".rs",
    ".go",
    ".java",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".v",
    ".vh",
    ".sv",
    ".svh",
    ".scala",
    ".rb",
    ".swift",
    ".kt",
)


@dataclass(frozen=True)
class ChunkRow:
    path: str
    chunk_index: int
    text: str
    tokens: int
    line_start: int | None = None
    line_end: int | None = None
    heading_prefix: str | None = None


@dataclass(frozen=True)
class ExpansionConfig:
    token_budget: int = 1024
    window: int = 8
    code_forward_bias: bool = True


class NeighborFetcher(Protocol):
    async def fetch_ranges(
        self, spans: Sequence[tuple[str, int, int]], zone_id: str | None
    ) -> list["ChunkRow"]: ...


def _is_code_path(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(ext) for ext in _CODE_EXTENSIONS)


def merge_spans(spans: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Merge overlapping/adjacent (path, lo, hi) spans into a minimal set."""
    by_path: dict[str, list[tuple[int, int]]] = {}
    for path, lo, hi in spans:
        by_path.setdefault(path, []).append((lo, hi))
    out: list[tuple[str, int, int]] = []
    for path, ranges in by_path.items():
        ranges.sort()
        clo, chi = ranges[0]
        for lo, hi in ranges[1:]:
            if lo <= chi + 1:
                chi = max(chi, hi)
            else:
                out.append((path, clo, chi))
                clo, chi = lo, hi
        out.append((path, clo, chi))
    return out


def _section_bounds(by_index: dict[int, ChunkRow], anchor_idx: int) -> tuple[int, int]:
    """Maximal contiguous run around anchor sharing its heading_prefix."""
    target = by_index[anchor_idx].heading_prefix
    lo = anchor_idx
    while (lo - 1) in by_index and by_index[lo - 1].heading_prefix == target:
        lo -= 1
    hi = anchor_idx
    while (hi + 1) in by_index and by_index[hi + 1].heading_prefix == target:
        hi += 1
    return lo, hi


def _window_for_anchor(
    by_index: dict[int, ChunkRow], anchor_idx: int, cfg: ExpansionConfig, is_code: bool
) -> tuple[int, int]:
    s_lo, s_hi = _section_bounds(by_index, anchor_idx)
    section_tokens = sum(by_index[i].tokens for i in range(s_lo, s_hi + 1) if i in by_index)
    if section_tokens <= cfg.token_budget:
        return s_lo, s_hi

    used = by_index[anchor_idx].tokens
    lo = hi = anchor_idx

    def _can(i: int) -> bool:
        return i in by_index and used + by_index[i].tokens <= cfg.token_budget

    if is_code and cfg.code_forward_bias:
        while hi + 1 <= s_hi and _can(hi + 1):
            hi += 1
            used += by_index[hi].tokens
        while lo - 1 >= s_lo and _can(lo - 1):
            lo -= 1
            used += by_index[lo].tokens
        return lo, hi

    back = True
    while True:
        moved = False
        if back and lo - 1 >= s_lo and _can(lo - 1):
            lo -= 1
            used += by_index[lo].tokens
            moved = True
        elif (not back) and hi + 1 <= s_hi and _can(hi + 1):
            hi += 1
            used += by_index[hi].tokens
            moved = True
        else:
            if back and hi + 1 <= s_hi and _can(hi + 1):
                hi += 1
                used += by_index[hi].tokens
                moved = True
            elif (not back) and lo - 1 >= s_lo and _can(lo - 1):
                lo -= 1
                used += by_index[lo].tokens
                moved = True
        if not moved:
            break
        back = not back
    return lo, hi


def _stitch(by_index: dict[int, ChunkRow], lo: int, hi: int) -> tuple[str, int | None, int | None]:
    rows = [by_index[i] for i in range(lo, hi + 1) if i in by_index]
    text = "\n".join(r.text for r in rows)
    starts = [r.line_start for r in rows if r.line_start is not None]
    ends = [r.line_end for r in rows if r.line_end is not None]
    return text, (min(starts) if starts else None), (max(ends) if ends else None)


async def expand_results(
    results: list,
    fetcher: NeighborFetcher,
    cfg: ExpansionConfig,
    zone_id: str | None = None,
) -> list:
    """Attach macro_text/macro_line_start/macro_line_end to each result. Best-effort."""
    if not results:
        return results

    spans = [
        (r.path, max(0, r.chunk_index - cfg.window), r.chunk_index + cfg.window) for r in results
    ]
    try:
        rows = await fetcher.fetch_ranges(merge_spans(spans), zone_id)
    except Exception:
        logger.warning("macro-chunk fetch failed; returning unexpanded", exc_info=True)
        return results

    by_path: dict[str, dict[int, ChunkRow]] = {}
    for row in rows:
        by_path.setdefault(row.path, {})[row.chunk_index] = row

    section_cache: dict[tuple[str, int, int], tuple[str, int | None, int | None]] = {}
    for r in results:
        by_index = by_path.get(r.path)
        if not by_index or r.chunk_index not in by_index:
            continue  # gap — leave chunk_text as-is
        try:
            w_lo, w_hi = _window_for_anchor(by_index, r.chunk_index, cfg, _is_code_path(r.path))
            key = (r.path, w_lo, w_hi)
            if key not in section_cache:
                section_cache[key] = _stitch(by_index, w_lo, w_hi)
            text, ls, le = section_cache[key]
            r.macro_text = text
            r.macro_line_start = ls
            r.macro_line_end = le
        except Exception:
            logger.warning("macro-chunk expansion failed for %s", r.path, exc_info=True)
            continue
    return results
