"""Prompt injection sanitization utilities (Issue #1756).

Provides defense-in-depth against prompt injection attacks (OWASP LLM01:2025):
1. ``sanitize_for_prompt`` — strips control tokens, null bytes, and enforces length.
2. ``detect_injection_patterns`` — scans for known injection patterns with severity.
3. ``wrap_untrusted_data`` — wraps text in XML tags for data-instruction separation.
4. ``enforce_injection_policy`` — applies configurable enforcement actions.

Usage::

    from nexus.security import (
        sanitize_for_prompt,
        detect_injection_patterns,
        wrap_untrusted_data,
    )

    clean = sanitize_for_prompt(user_input)
    warnings = detect_injection_patterns(user_input)
    tagged = wrap_untrusted_data(file_content, "FILE_CONTENT")
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.security.policy import InjectionAction, InjectionPolicyConfig

logger = logging.getLogger(__name__)

# Maximum length for prompt text (10KB default)
MAX_PROMPT_LENGTH = 10_000

# Pre-compiled regex for zero-width Unicode characters used for obfuscation.
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")

# Pre-compiled patterns for injection detection.
# Each tuple: (pattern_name, severity, compiled_regex)
# Severity levels: "high", "medium", "low"
_INJECTION_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # HIGH severity — direct prompt manipulation
    (
        "instruction_override",
        "high",
        re.compile(r"(?i)(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?))"),
    ),
    (
        "delimiter_escape",
        "high",
        re.compile(r"(?i)(```\s*system|<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\])"),
    ),
    (
        "xml_tag_injection",
        "high",
        re.compile(r"</(USER_DATA|FILE_CONTENT|MEMORY_\d+_CONTENT)>"),
    ),
    (
        "token_smuggling",
        "high",
        re.compile(r"(?i)(\\n\\n|<\|endoftext\|>|<\|padding\|>)"),
    ),
    # MEDIUM severity — role/behavior modification
    (
        "role_override",
        "medium",
        re.compile(
            r"(?i)\b(you\s+are\s+now\b|you\s+are\s+an?\s|act\s+as\b|pretend\s+to\s+be|role-?play)\b"
        ),
    ),
    (
        "system_prompt_extraction",
        "medium",
        re.compile(r"(?i)(system\s+prompt|initial\s+instruction|original\s+prompt)"),
    ),
    (
        "new_instruction",
        "medium",
        re.compile(r"(?i)(new\s+instruction|instead\s+do|disregard\s+and)"),
    ),
    (
        "jailbreak_dan",
        "medium",
        re.compile(r"(?i)\bDAN\b.*\b(mode|jailbreak|do\s+anything)\b"),
    ),
    (
        "jailbreak_developer",
        "medium",
        re.compile(
            r"(?i)(developer\s+mode|maintenance\s+mode|debug\s+mode)\s*(enabled|on|activated)"
        ),
    ),
    (
        "prompt_leak",
        "medium",
        re.compile(r"(?i)(repeat|print|show|reveal|output)\s+.{0,20}(prompt|instructions?)\b"),
    ),
    (
        "encoding_bypass",
        "medium",
        re.compile(r"(?i)(base64|rot13|hex)\s*(encode|decode|the\s+following)"),
    ),
    # LOW severity — indirect/subtle manipulation
    (
        "multi_step",
        "low",
        re.compile(
            r"(?i)(step\s*1\s*:.*step\s*2\s*:.*step\s*3\s*:|first\s*,.*then\s*,.*finally\s*,)"
        ),
    ),
    (
        "markdown_injection",
        "low",
        re.compile(r"!\[[^\]]*\]\(https?://[^)]*[?&][^)]+\)"),
    ),
    (
        "unicode_obfuscation",
        "low",
        _ZERO_WIDTH_RE,
    ),
]

# Control characters to strip (null bytes, BEL, backspace, etc.)
# Preserve \n (0x0A), \r (0x0D), \t (0x09)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_for_prompt(text: str, max_length: int = MAX_PROMPT_LENGTH) -> str:
    """Strip control tokens, null bytes, and truncate to max_length.

    Args:
        text: Raw text to sanitize.
        max_length: Maximum allowed length. Defaults to MAX_PROMPT_LENGTH (10KB).

    Returns:
        Sanitized text safe for LLM prompt inclusion.
    """
    if not isinstance(text, str):
        return str(text)

    # Strip null bytes and control characters (preserve newlines, tabs, CR)
    cleaned = _CONTROL_CHAR_RE.sub("", text)

    # Strip zero-width Unicode characters used for obfuscation
    cleaned = _ZERO_WIDTH_RE.sub("", cleaned)

    # Truncate to max length
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
        logger.warning(
            "Prompt text truncated from %d to %d characters",
            len(text),
            max_length,
        )

    return cleaned


def detect_injection_patterns(text: str) -> list[tuple[str, str]]:
    """Scan text for known injection patterns.

    Returns a list of (pattern_name, severity) tuples for logging/alerting.
    Does NOT modify the text — use sanitize_for_prompt() for that.

    Args:
        text: Text to scan.

    Returns:
        List of (pattern_name, severity) tuples (empty if clean).
    """
    if not text:
        return []

    matched: list[tuple[str, str]] = []
    for pattern_name, severity, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            matched.append((pattern_name, severity))

    return matched


def wrap_untrusted_data(text: str, label: str = "USER_DATA") -> str:
    """Wrap text in XML tags for data-instruction separation.

    This is the OWASP-recommended approach for separating untrusted data
    from LLM instructions. The LLM system prompt should instruct the model
    to treat content within these tags as data only.

    Args:
        text: Untrusted text to wrap.
        label: XML tag label. Defaults to "USER_DATA".

    Returns:
        Text wrapped in XML tags with sanitization applied.
    """
    sanitized = sanitize_for_prompt(text)

    # Escape both opening AND closing tags that match our label to prevent tag injection
    escaped = sanitized.replace(f"</{label}>", f"&lt;/{label}&gt;")
    escaped = escaped.replace(f"<{label}>", f"&lt;{label}&gt;")

    return f"<{label}>\n{escaped}\n</{label}>"


def enforce_injection_policy(
    text: str,
    policy: InjectionPolicyConfig | None = None,
) -> tuple[bool, list[tuple[str, str]]]:
    """Apply configurable enforcement against detected injection patterns.

    Returns (allowed, detections) where allowed=False means the request
    should be blocked based on the policy configuration.

    Args:
        text: Text to scan.
        policy: Injection policy config. If None, defaults to LOG-only.

    Returns:
        Tuple of (allowed: bool, detections: list[(name, severity)]).
    """
    from nexus.security.policy import InjectionAction, InjectionPolicyConfig

    if policy is None:
        policy = InjectionPolicyConfig()

    detections = detect_injection_patterns(text)
    if not detections:
        return True, []

    allowed = True
    for name, severity in detections:
        action = _get_action_for_severity(policy, severity)
        if action == InjectionAction.BLOCK:
            logger.warning(
                "Injection BLOCKED: pattern=%s severity=%s",
                name,
                severity,
            )
            allowed = False
        elif action == InjectionAction.ESCALATE:
            logger.warning(
                "Injection ESCALATED: pattern=%s severity=%s",
                name,
                severity,
            )
            if policy.escalation_callback is not None:
                policy.escalation_callback(text, detections)
            # Escalate does not block by default
        else:
            # LOG only
            logger.info(
                "Injection detected (log-only): pattern=%s severity=%s",
                name,
                severity,
            )

    return allowed, detections


def _get_action_for_severity(policy: InjectionPolicyConfig, severity: str) -> InjectionAction:
    """Get the configured action for a given severity level."""
    severity_map = {
        "high": policy.high_severity_action,
        "medium": policy.medium_severity_action,
        "low": policy.low_severity_action,
    }
    return severity_map.get(severity, policy.default_action)
