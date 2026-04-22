"""Shared utilities for Gmail CLI connector operations.

Extracted to give both GmailConnector (gws CLI wrapper) and the native
Gmail connector a single implementation of MIME body extraction.
"""

from __future__ import annotations

import base64
from typing import Any


def extract_body(payload: dict[str, Any]) -> str:
    """Walk a Gmail API payload tree and extract the message body.

    Prefers ``text/plain``; falls back to ``text/html``.  Recursively
    descends ``multipart/*`` containers.  Returns an empty string if no
    body part is found.

    Gmail encodes body data as URL-safe base64 without padding; the ``==``
    suffix makes ``urlsafe_b64decode`` tolerant of missing padding bytes.

    Args:
        payload: The ``payload`` dict from a ``messages.get`` response with
            ``format=full``.

    Returns:
        Decoded message body text, or ``""`` if not present.
    """
    html_fallback: str | None = None

    def _walk(part: dict[str, Any]) -> str | None:
        nonlocal html_fallback
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            data = (part.get("body") or {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
        if mime == "text/html" and html_fallback is None:
            data = (part.get("body") or {}).get("data", "")
            if data:
                html_fallback = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
        for sub in part.get("parts") or []:
            found = _walk(sub)
            if found:
                return found
        return None

    result = _walk(payload)
    return result or html_fallback or ""
