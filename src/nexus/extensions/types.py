"""Pure data types shared by the manifest contract and connector code.

This module is foundational: nexus.extensions.manifest, nexus.extensions.store,
and nexus.backends.* all depend on it. It depends on nothing in the nexus
package — keep it that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

Kind = Literal["connector", "brick", "plugin"]


class ArgType(Enum):
    """Types for connection arguments."""

    STRING = "string"
    SECRET = "secret"
    PASSWORD = "password"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    PATH = "path"
    OAUTH = "oauth"


@dataclass
class ConnectionArg:
    """Definition of a connection argument for a connector."""

    type: ArgType
    description: str
    required: bool = True
    default: Any = None
    secret: bool = False
    audit_safe: bool = False
    """Mark `True` when a non-secret field's name matches the secret-shape heuristic
    (key/secret/token/password/cred) but the value is provably non-sensitive.
    The portability redaction audit (Issue #4083) uses this to allow declared
    false positives without weakening the heuristic. Use sparingly and document
    the reason in the ConnectionArg description."""
    env_var: str | None = None
    config_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.type.value,
            "description": self.description,
            "required": self.required,
            "default": self.default,
            "secret": self.secret,
            "audit_safe": self.audit_safe,
            "env_var": self.env_var,
        }
        if self.config_key is not None:
            result["config_key"] = self.config_key
        return result
