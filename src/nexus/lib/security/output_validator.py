"""LLM output validation for leaked prompts and credentials (Issue #1756).

Provides post-generation validation to detect when an LLM response may
contain leaked system prompts or credential patterns.

Usage::

    from nexus.security import validate_llm_output

    warnings = validate_llm_output(response, system_prompt=system_prompt)
    if warnings:
        logger.warning("Output validation warnings: %s", warnings)
"""

from __future__ import annotations

import re

# Pre-compiled credential patterns
_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key_sk", re.compile(r"\bsk-[a-zA-Z0-9]{20,}")),
    ("api_key_prefix", re.compile(r"\b(api[_-]?key|apikey)\s*[=:]\s*\S{8,}", re.IGNORECASE)),
    ("password_field", re.compile(r"\b(password|passwd|pwd)\s*[=:]\s*\S{4,}", re.IGNORECASE)),
    ("bearer_token", re.compile(r"\bBearer\s+[a-zA-Z0-9._\-]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[A-Z0-9]{12,}")),
    ("private_key", re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----")),
]


def validate_llm_output(
    response: str,
    system_prompt: str | None = None,
) -> list[str]:
    """Check LLM response for leaked prompts or credentials.

    This is a post-generation check. It does NOT modify the response —
    callers decide how to handle warnings (log, redact, block).

    Args:
        response: The LLM-generated response text.
        system_prompt: Optional system prompt to check for echo/leak.

    Returns:
        List of warning strings (empty if clean).
    """
    if not response:
        return []

    warnings: list[str] = []

    # 1. Check for system prompt echo
    if system_prompt and len(system_prompt) >= 20:
        # Use a sliding window to detect substantial overlap
        # Check if a significant portion of the system prompt appears in the response
        chunk_size = min(50, len(system_prompt))
        for i in range(0, len(system_prompt) - chunk_size + 1, chunk_size // 2):
            chunk = system_prompt[i : i + chunk_size]
            if chunk in response:
                warnings.append(
                    f"system_prompt_echo: response contains system prompt fragment "
                    f"(offset {i}, length {chunk_size})"
                )
                break

    # 2. Check for credential patterns
    for name, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(response):
            warnings.append(f"credential_leak: {name}")

    return warnings
