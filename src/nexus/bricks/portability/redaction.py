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

Audit fails if a CONNECTION_ARGS key matches this regex but has neither
secret=True nor audit_safe=True. The check is deliberately strict — every
false positive should be either renamed, marked secret=True, or marked
audit_safe=True with justification in the ConnectionArg description.
See `audit_backend` for the enforcement logic."""


def _get_connection_args(backend_type: str) -> dict[str, Any]:
    """Return the CONNECTION_ARGS dict for `backend_type`, or {} if unavailable.

    Returns {} for placeholder registry entries whose connector module failed
    to import (extra not installed) — those are skipped by callers, not
    treated as audit failures.

    Looks in two places, in order:
    1. Class attribute `<connector_class>.CONNECTION_ARGS` (legacy style;
       used by storage backends and most OAuth connectors).
    2. ConnectorManifest.connection_args from the extension-store manifest
       (the #3964 path; Slack and future external-plugin connectors live here).

    Note: similar to ConnectorRegistry.get_connection_args, but kept local so
    tests can patch this single function to inject fake CONNECTION_ARGS without
    monkey-patching the global registry.

    Forces ``_register_optional_backends()`` once at first call. Without this
    the registry only contains backends imported by other code paths so far
    (often just ``cas_local`` and ``path_local`` in a fresh server process),
    and ``ConnectorRegistry.get_info("path_s3")`` would raise ``KeyError`` —
    which would surface to the operator as ``Unknown connector 'path_s3'``
    instead of either a clean redaction or an honest "extra not installed"
    skip. The registration is idempotent; subsequent calls are a no-op.
    """
    from nexus.backends import _register_optional_backends
    from nexus.backends.base.registry import ConnectorRegistry

    _register_optional_backends()
    try:
        info = ConnectorRegistry.get_info(backend_type)
    except KeyError:
        return {}
    cls = info.connector_class
    args = getattr(cls, "CONNECTION_ARGS", {}) or {} if cls is not None else {}
    if args:
        return args

    # Fallback: extension-store manifest (Slack uses this path).
    try:
        from nexus.extensions.manifest import ConnectorManifest
        from nexus.extensions.store import get_store

        manifest = get_store().get(backend_type, "connector")
        # The store can return non-connector kinds; only ConnectorManifest
        # carries `connection_args` — narrow before access.
        if isinstance(manifest, ConnectorManifest):
            return dict(manifest.connection_args or {})
    except Exception:
        # Extension store may not be initialised in all test contexts.
        pass

    return {}


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
        SensitiveFieldNotDeclaredError: if audit_backend(backend_type) is non-empty
            (a real leak — backend has a secret-shaped field not marked secret/
            audit_safe), OR if the backend's CONNECTION_ARGS cannot be resolved
            (registry doesn't know it, or its connector class failed to import
            for missing optional extras). The latter is treated as a leak risk:
            we cannot confidently strip a contract we can't introspect, so the
            export is refused rather than silently shipping cleartext credentials.
    """
    args = _get_connection_args(backend_type)
    if not args:
        # Backend not in registry, or connector class failed to import (e.g.,
        # boto3 not installed on a slim server). We can't introspect the
        # CONNECTION_ARGS contract, so we don't know which fields are secret —
        # refuse the export rather than ship cleartext credentials. Operators
        # see the offending fields in the error so they can install the
        # missing extra (e.g., `pip install nexus-fs[s3]`) and retry.
        raise SensitiveFieldNotDeclaredError(
            backend_type=f"{backend_type} (CONNECTION_ARGS not loadable; "
            f"install the matching extra so the redaction contract resolves)",
            fields=sorted(config.keys()),
        )

    leaks = audit_backend(backend_type)
    if leaks:
        raise SensitiveFieldNotDeclaredError(backend_type=backend_type, fields=leaks)

    # Round-3 reviewer finding: persisted backend_config can contain
    # keys that aren't declared in CONNECTION_ARGS (a misconfigured
    # mount, or a backend that accepts **kwargs). The declared-secrets
    # pass below would silently ship those. Audit any undeclared key
    # whose name matches the secret-shape regex and raise rather than
    # leak. This also catches nested dict/list values whose contents
    # look like secrets (e.g., a `metadata` blob containing
    # `access_key`).
    config_leaks = _scan_config_for_undeclared_secrets(args, config)
    if config_leaks:
        raise SensitiveFieldNotDeclaredError(
            backend_type=f"{backend_type} (undeclared secret-shaped fields "
            f"in persisted backend_config)",
            fields=config_leaks,
        )

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


def _scan_config_for_undeclared_secrets(
    args: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    """Return dotted paths of keys in `config` that look secret but are not
    declared in `args`. Walks nested dicts/lists so a secret hiding under an
    arbitrary `metadata` blob still surfaces.

    Only secret-shaped *keys* are flagged. Values themselves aren't pattern-
    matched here — we trust the connector author to declare structure
    via CONNECTION_ARGS. The check is conservative: any undeclared key whose
    name matches the heuristic, OR any nested dict/list key matching the
    heuristic regardless of declaration (because nested keys are never in
    CONNECTION_ARGS), is reported.
    """
    declared_top_level: set[str] = set(args.keys())
    leaks: list[str] = []

    def _walk(value: Any, path: str, *, top_level: bool) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if not isinstance(k, str):
                    continue
                here = f"{path}.{k}" if path else k
                # At top level, an undeclared secret-shaped key is a leak.
                # Below top level, ANY secret-shaped key is a leak (nested
                # secrets can't be redacted via CONNECTION_ARGS contract).
                is_secret_shaped = bool(SECRET_SHAPED.search(k))
                if is_secret_shaped and (
                    (top_level and k not in declared_top_level) or not top_level
                ):
                    leaks.append(here)
                _walk(v, here, top_level=False)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                _walk(item, f"{path}[{i}]", top_level=False)

    _walk(config, "", top_level=True)
    return sorted(leaks)


__all__ = [
    "SECRET_SHAPED",
    "declared_secret_fields",
    "audit_backend",
    "redact_config",
]
