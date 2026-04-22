"""CLI subprocess result types and error mapping.

Provides structured handling of CLI subprocess outcomes with
pattern-based error classification for agent-friendly error messages.

Design decisions (Issue #3148):
    - CLIResult covers 5 failure modes: not installed, exit code, bad output,
      timeout, auth expired (Decision #6)
    - CLIErrorMapper uses stderr pattern matching for error classification (6A)
    - Error patterns are data (configurable per connector), not code
    - Maps to existing ValidationError / ErrorDef system in base.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CLIResultStatus(StrEnum):
    """Status of a CLI subprocess execution."""

    SUCCESS = "success"
    NOT_INSTALLED = "not_installed"
    EXIT_ERROR = "exit_error"
    BAD_OUTPUT = "bad_output"
    TIMEOUT = "timeout"
    AUTH_EXPIRED = "auth_expired"


@dataclass(frozen=True)
class CLIResult:
    """Structured result from a CLI subprocess execution.

    Captures stdout, stderr, exit code, and classified status so callers
    can handle errors uniformly without parsing raw subprocess output.

    Attributes:
        status: Classified outcome of the subprocess call.
        exit_code: Process exit code (None if not started or timed out).
        stdout: Standard output (may be partial on failure).
        stderr: Standard error output.
        command: The command that was executed (for diagnostics).
        duration_ms: Execution time in milliseconds.
        error_code: Mapped error code from CLIErrorMapper (if applicable).
        retryable: Whether the error is retryable.
    """

    status: CLIResultStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    error_code: str | None = None
    retryable: bool = False

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""
        return self.status == CLIResultStatus.SUCCESS

    def as_json(self) -> Any:
        """Parse stdout as JSON, stripping any CLI preamble before the first ``{`` or ``[``.

        Raises:
            ValueError: If stdout cannot be parsed as JSON.
        """
        import json as _json

        text = self.stdout
        # Find the first JSON start character — object or array.
        obj_idx = text.find("{")
        arr_idx = text.find("[")
        candidates = [i for i in (obj_idx, arr_idx) if i >= 0]
        if candidates:
            start = min(candidates)
            if start > 0:
                text = text[start:]
        try:
            return _json.loads(text)
        except Exception as exc:
            raise ValueError(f"Failed to parse CLI output as JSON: {exc}") from exc

    def as_yaml(self) -> Any:
        """Parse stdout as YAML, stripping any CLI preamble lines.

        Skips leading lines that don't look like YAML content (e.g. ``Using
        keyring backend…``).  Recognises the three common YAML document starts:
        ``key:`` (mapping), ``- `` or ``-\n`` (sequence), and ``---`` (document
        marker).  Then parses with ``yaml.safe_load``.

        Raises:
            ValueError: If stdout cannot be parsed as YAML.
        """
        import re as _re

        import yaml as _yaml

        text = self.stdout
        # Advance past any preamble to the first line that looks like YAML:
        # a mapping key, a sequence item, or a document-start marker.
        match = _re.search(
            r"^(?:[a-zA-Z_]\w*:|---|- |-$)",
            text,
            _re.MULTILINE,
        )
        if match and match.start() > 0:
            text = text[match.start() :]
        try:
            parsed = _yaml.safe_load(text)
        except Exception as exc:
            raise ValueError(f"Failed to parse CLI output as YAML: {exc}") from exc
        if not isinstance(parsed, (dict, list)):
            raise ValueError(
                f"CLI output parsed as YAML scalar ({type(parsed).__name__!r}), "
                "expected a mapping or sequence"
            )
        return parsed

    def summary(self) -> str:
        """Human-readable summary for logging."""
        cmd_str = " ".join(self.command[:3])
        if self.ok:
            return f"{cmd_str}: OK ({self.duration_ms:.0f}ms)"
        msg = f"{cmd_str}: {self.status.value}"
        if self.exit_code is not None:
            msg += f" (exit={self.exit_code})"
        if self.error_code:
            msg += f" [{self.error_code}]"
        if self.stderr:
            # First line of stderr, truncated
            first_line = self.stderr.strip().split("\n")[0][:120]
            msg += f" — {first_line}"
        return msg


@dataclass(frozen=True)
class ErrorMapping:
    """Maps a CLI error pattern to a structured error classification.

    Attributes:
        code: Error code for the agent (e.g., "RATE_LIMITED").
        retryable: Whether the agent should retry.
        backoff: Backoff strategy ("none", "linear", "exponential").
        action: Suggested recovery action (e.g., "reauth", "wait").
    """

    code: str
    retryable: bool = False
    backoff: str = "none"
    action: str | None = None


# Default error patterns — connectors can override/extend via config.
_DEFAULT_ERROR_PATTERNS: list[tuple[str, ErrorMapping]] = [
    (
        r"429|rate.limit|too many requests|quota exceeded",
        ErrorMapping(code="RATE_LIMITED", retryable=True, backoff="exponential"),
    ),
    (
        r"401|unauthorized|token expired|invalid.credentials|unauthenticated",
        ErrorMapping(code="AUTH_EXPIRED", retryable=True, action="reauth"),
    ),
    (
        r"403|forbidden|permission.denied|access.denied",
        ErrorMapping(code="PERMISSION_DENIED", retryable=False),
    ),
    (
        r"404|not found|does not exist",
        ErrorMapping(code="NOT_FOUND", retryable=False),
    ),
    (
        r"409|conflict|already exists|duplicate",
        ErrorMapping(code="CONFLICT", retryable=False),
    ),
    (
        r"5\d{2}|internal.error|server.error|service.unavailable",
        ErrorMapping(code="SERVER_ERROR", retryable=True, backoff="exponential"),
    ),
    (
        r"timeout|timed.out|deadline.exceeded",
        ErrorMapping(code="TIMEOUT", retryable=True, backoff="linear"),
    ),
    (
        r"network|connection.refused|connection.reset|dns",
        ErrorMapping(code="NETWORK_ERROR", retryable=True, backoff="linear"),
    ),
]


class CLIErrorMapper:
    """Maps CLI subprocess errors to structured error classifications.

    Uses pattern matching against stderr + exit code to classify errors.
    Connectors provide custom patterns via their declarative config;
    defaults cover common HTTP-like error patterns.

    Example::

        mapper = CLIErrorMapper(extra_patterns=[
            (r"mailbox.full", ErrorMapping(code="MAILBOX_FULL", retryable=False)),
        ])
        result = mapper.classify(exit_code=1, stderr="429 Too Many Requests")
        assert result.code == "RATE_LIMITED"
        assert result.retryable is True
    """

    def __init__(
        self,
        extra_patterns: list[tuple[str, ErrorMapping]] | None = None,
    ) -> None:
        # Custom patterns take priority over defaults
        self._patterns: list[tuple[re.Pattern[str], ErrorMapping]] = []
        for pattern_str, mapping in extra_patterns or []:
            self._patterns.append((re.compile(pattern_str, re.IGNORECASE), mapping))
        for pattern_str, mapping in _DEFAULT_ERROR_PATTERNS:
            self._patterns.append((re.compile(pattern_str, re.IGNORECASE), mapping))

    def classify(
        self,
        exit_code: int | None,
        stderr: str,
        stdout: str = "",
    ) -> ErrorMapping | None:
        """Classify a CLI error based on exit code and output.

        Args:
            exit_code: Process exit code.
            stderr: Standard error output.
            stdout: Standard output (checked as fallback).

        Returns:
            Matched ErrorMapping, or None if no pattern matches.
        """
        if exit_code == 0:
            return None

        # Check stderr first, then stdout as fallback
        combined = f"{stderr}\n{stdout}"
        for pattern, mapping in self._patterns:
            if pattern.search(combined):
                return mapping

        return None

    def classify_result(self, result: CLIResult) -> CLIResult:
        """Enrich a CLIResult with error classification.

        Returns a new CLIResult with error_code and retryable set based
        on pattern matching. If no pattern matches, returns the original.
        """
        if result.ok:
            return result

        mapping = self.classify(result.exit_code, result.stderr, result.stdout)
        if mapping is None:
            return result

        # Determine status from mapping
        status = result.status
        if mapping.code == "AUTH_EXPIRED":
            status = CLIResultStatus.AUTH_EXPIRED
        elif mapping.code == "TIMEOUT":
            status = CLIResultStatus.TIMEOUT

        return CLIResult(
            status=status,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            command=result.command,
            duration_ms=result.duration_ms,
            error_code=mapping.code,
            retryable=mapping.retryable,
        )

    def to_dict(self) -> list[dict[str, Any]]:
        """Serialize patterns for diagnostics/config export."""
        return [
            {
                "pattern": p.pattern,
                "code": m.code,
                "retryable": m.retryable,
                "backoff": m.backoff,
                "action": m.action,
            }
            for p, m in self._patterns
        ]
