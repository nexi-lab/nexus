"""Neutral auth contracts shared across CLI, services, and connectors.

Keeps non-OAuth credentials out of OAuth-specific types while preserving
explicit status and resolution metadata for UX and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CredentialKind(StrEnum):
    """Credential storage strategy."""

    OAUTH = "oauth"
    SECRET = "secret"
    NATIVE = "native"


class AuthStatus(StrEnum):
    """Normalized auth status for UX surfaces."""

    AUTHED = "authed"
    EXPIRED = "expired"
    NO_AUTH = "no_auth"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SecretCredentialRecord:
    """Stored non-OAuth credential metadata plus secret payload."""

    service: str
    kind: CredentialKind
    data: dict[str, str] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class AuthSummary:
    """Redacted summary shown by auth list / status surfaces."""

    service: str
    kind: CredentialKind
    status: AuthStatus
    source: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthResolution:
    """Result of backend credential resolution."""

    service: str
    status: AuthStatus
    source: str
    resolved_config: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
