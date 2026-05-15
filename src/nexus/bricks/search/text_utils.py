"""Shared text utilities for search indexing (Issue #3725).

Centralises the camelCase/snake_case/path tokenisation logic that was
previously duplicated across chunking.py and other search modules.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Token-splitting patterns (camelCase lives here, not in chunking.py)
# ---------------------------------------------------------------------------

# Splits on camelCase boundaries: parseUserAuth → parse User Auth
_CAMEL_SPLIT_RE = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"  # lower/digit followed by upper
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # e.g. "HTMLParser" → "HTML" "Parser"
)

# Characters that act as word separators in file names and paths
_SEPARATOR_RE = re.compile(r"[/_\-\.\s]+")


def tokenize_path(path: str) -> str:
    """Tokenize a virtual path into a space-separated lowercase word string.

    Splits on:
    - Path separators: /
    - Word separators in identifiers: _ - . (space)
    - camelCase boundaries: parseUserAuth → parse user auth

    The result is suitable for feeding into the BM25S index as a document
    field.  The calling code should pass this alongside the title as separate
    BM25S columns so per-field weights can be applied.

    Examples:
        >>> tokenize_path("/workspace/src/auth/parseUserLogin.py")
        'workspace src auth parse user login py'
        >>> tokenize_path("/docs/README_API.md")
        'docs readme api md'
    """
    if not path:
        return ""

    # 1. Split on path/separator characters first
    parts = _SEPARATOR_RE.split(path)

    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        # 2. Split camelCase within each segment
        sub_parts = _CAMEL_SPLIT_RE.split(part)
        tokens.extend(sp.lower() for sp in sub_parts if sp)

    return " ".join(tokens)
