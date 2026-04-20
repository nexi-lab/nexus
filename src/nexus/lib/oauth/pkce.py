"""RFC 7636 PKCE helpers.

S256-only. The plain ``code_challenge_method`` is not supported because every
OAuth 2.1 server allows S256 and some servers reject ``plain``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

__all__ = ["generate_pkce_pair", "make_code_challenge"]


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)``.

    ``code_verifier`` is a 32-byte urlsafe-base64 string without padding
    (43 chars), satisfying RFC 7636 §4.1. ``code_challenge`` is
    ``SHA256(verifier)`` urlsafe-base64 without padding.
    """
    verifier = secrets.token_urlsafe(32)
    return verifier, make_code_challenge(verifier)


def make_code_challenge(verifier: str) -> str:
    """Compute the S256 code challenge for ``verifier``."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
