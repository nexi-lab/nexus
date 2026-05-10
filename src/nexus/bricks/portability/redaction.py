"""Typed redaction contract for mount-config exports (Issue #4083).

Single source of truth: each connector's CONNECTION_ARGS already declares
`secret: bool` per argument. This module derives the redaction set from
that declaration, and runs a heuristic audit that hard-fails export when a
secret-shaped argument name (key/secret/token/password/cred) isn't marked
secret=True.

Why a separate module: the redaction policy is the contract surface that
both export and import depend on. Keeping it focused (one file, three
public functions) means the audit test, the export pipeline, and any
future tooling all see the same answer to "is this field a secret?".
"""

from __future__ import annotations

import re
from typing import Any

from nexus.bricks.portability.models import (
    PlaceholderRef,
    SensitiveFieldNotDeclaredError,
)

SECRET_SHAPED = re.compile(r"(?i)(key|secret|token|password|cred)")
"""Heuristic regex for argument names that should be marked secret=True.

Audit fails if a CONNECTION_ARGS key matches this regex but has secret=False.
The check is deliberately strict — every false positive is a forcing function
to either rename the field or mark it secret=True. No allowlist exists today;
add one only if real friction emerges (see spec for `audit_safe` follow-up)."""


def _get_connection_args(backend_type: str) -> dict[str, Any]:
    """Return the CONNECTION_ARGS dict for `backend_type`, or {} if unavailable.

    Returns {} for placeholder registry entries whose connector module failed
    to import (extra not installed) — those are skipped by callers, not
    treated as audit failures.

    Note: similar to ConnectorRegistry.get_connection_args, but kept local so
    tests can patch this single function to inject fake CONNECTION_ARGS without
    monkey-patching the global registry.
    """
    from nexus.backends.base.registry import ConnectorRegistry

    info = ConnectorRegistry.get_info(backend_type)
    cls = info.connector_class
    if cls is None:
        return {}
    return getattr(cls, "CONNECTION_ARGS", {}) or {}


def declared_secret_fields(backend_type: str) -> frozenset[str]:
    """Return the set of CONNECTION_ARGS keys with secret=True for a backend."""
    args = _get_connection_args(backend_type)
    return frozenset(name for name, arg in args.items() if getattr(arg, "secret", False))


def audit_backend(backend_type: str) -> list[str]:
    """Return CONNECTION_ARGS keys that look secret-shaped but aren't marked secret=True.

    Empty list = backend passes audit. Non-empty = export must abort.
    Returns [] for backends whose connector class hasn't loaded (no audit possible
    until the optional extra is installed; the audit test skips those entries).

    Escape hatch: set ``audit_safe=True`` on a ``ConnectionArg`` to suppress the
    heuristic for a provably non-sensitive field whose name happens to match the
    secret-shape regex (e.g. ``token_manager_db`` is a filesystem path, not a
    credential). Document the reason in the field's description.
    """
    args = _get_connection_args(backend_type)
    return [
        name
        for name, arg in args.items()
        if SECRET_SHAPED.search(name)
        and not getattr(arg, "secret", False)
        and not getattr(arg, "audit_safe", False)
    ]


def redact_config(
    backend_type: str,
    config: dict[str, Any],
    *,
    mount_id: str,
) -> tuple[dict[str, Any], list[PlaceholderRef]]:
    """Strip declared secret fields from a mount's backend_config.

    Args:
        backend_type: Connector registry key (e.g., "path_s3").
        config: The mount's backend_config dict.
        mount_id: Used to namespace placeholders (uniqueness across bundle).

    Returns:
        (redacted_config, placeholders). The redacted dict is a shallow copy with
        secret fields replaced by `${MOUNT_<id>_<FIELD_UPPER>}`. None values are
        skipped (no placeholder generated for a field that's None).

    Raises:
        SensitiveFieldNotDeclaredError: if audit_backend(backend_type) is non-empty.
    """
    leaks = audit_backend(backend_type)
    if leaks:
        raise SensitiveFieldNotDeclaredError(backend_type=backend_type, fields=leaks)

    secrets = declared_secret_fields(backend_type)
    out = dict(config)
    placeholders: list[PlaceholderRef] = []

    for field_name in secrets & out.keys():
        value = out[field_name]
        if value is None:
            continue
        ph_name = f"MOUNT_{mount_id}_{field_name.upper()}"
        placeholder_string = f"${{{ph_name}}}"
        if value == placeholder_string:
            continue
        out[field_name] = placeholder_string
        placeholders.append(PlaceholderRef(name=ph_name, field=f"mounts.{mount_id}.{field_name}"))

    return out, placeholders


__all__ = [
    "SECRET_SHAPED",
    "declared_secret_fields",
    "audit_backend",
    "redact_config",
]
