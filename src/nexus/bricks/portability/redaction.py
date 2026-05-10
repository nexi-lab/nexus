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


def _backend_is_registered(backend_type: str) -> bool:
    """Return True if `backend_type` is in the ConnectorRegistry (with or
    without a loaded connector_class). Used by ``redact_config`` to
    distinguish 'unknown backend' from 'registered backend with no
    CONNECTION_ARGS' — the latter is legitimate for CLI/YAML-defined
    custom connectors, the former is a leak risk."""
    from nexus.backends import _register_optional_backends
    from nexus.backends.base.registry import ConnectorRegistry

    _register_optional_backends()
    try:
        ConnectorRegistry.get_info(backend_type)
        return True
    except KeyError:
        return False


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
        # Two cases produce empty args; only one is a leak risk:
        #
        # (a) backend_type is NOT in the ConnectorRegistry at all (e.g.
        #     unknown name, slim install missing the matching extra so
        #     the connector class never loaded). We can't introspect any
        #     contract → refuse the export rather than ship cleartext.
        #
        # (b) backend_type IS registered but has no CONNECTION_ARGS
        #     declared (CLI/YAML-defined custom connectors don't always
        #     define the class attribute). Round-4 reviewer flagged the
        #     blanket refuse path as too strict — it broke supported
        #     workflows for these. For (b) we still scan the persisted
        #     backend_config for secret-shaped keys and refuse if any
        #     are found; otherwise allow export with no placeholders.
        if not _backend_is_registered(backend_type):
            raise SensitiveFieldNotDeclaredError(
                backend_type=f"{backend_type} (unknown backend; CONNECTION_ARGS "
                f"not loadable — install the matching extra or register the "
                f"connector so the redaction contract resolves)",
                fields=sorted(config.keys()),
            )
        # Registered backend with no CONNECTION_ARGS contract. Round-4
        # passed this through after a key-only scan, but round-5 reviewer
        # flagged that values can carry secrets in innocuous keys (e.g.,
        # ``command='aws ... --secret-key=AKIA...'`` or a DSN URL with
        # userinfo). Without a connector-declared schema we have no way
        # to know which value substrings to strip, so pattern-scan keys
        # AND values, and refuse export if anything looks like a
        # credential. The connector author's escape hatch is to declare
        # CONNECTION_ARGS or add ``audit_safe`` markers.
        config_leaks = _scan_config_for_undeclared_secrets({}, config)
        value_leaks = _scan_config_values_for_secret_patterns(config)
        all_leaks = sorted(set(config_leaks) | set(value_leaks))
        if all_leaks:
            raise SensitiveFieldNotDeclaredError(
                backend_type=f"{backend_type} (registered but declares no "
                f"CONNECTION_ARGS; cannot safely export — values look "
                f"credential-shaped or keys match the secret heuristic. "
                f"Declare CONNECTION_ARGS with secret/audit_safe flags or "
                f"omit this mount with --no-include-mounts)",
                fields=all_leaks,
            )
        # No declared secrets, no scanned leaks → pass through unchanged.
        return dict(config), []

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

    # Round-6 reviewer finding: audit_safe-marked or non-secret declared
    # fields can still hold credential-shaped VALUES (e.g.,
    # ``token_manager_db = "postgresql://user:pass@host/db"`` — a
    # filesystem-style audit_safe field that happens to carry a real
    # DB URL with userinfo). Run the value-pattern scan over the
    # non-secret declared fields too, refusing if any look credential-
    # bearing. The connector author's recourse is to mark the field
    # secret=True (will be redacted) or sanitize the value before save.
    secrets = declared_secret_fields(backend_type)
    non_secret_subset = {k: v for k, v in config.items() if k not in secrets}
    value_leaks = _scan_config_values_for_secret_patterns(non_secret_subset)
    if value_leaks:
        raise SensitiveFieldNotDeclaredError(
            backend_type=f"{backend_type} (non-secret declared field carries a "
            f"credential-shaped value — mark the field secret=True or "
            f"sanitize the value before persisting the mount)",
            fields=value_leaks,
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


# Patterns of credential-shaped VALUES that should not appear in an
# exported mount config when no CONNECTION_ARGS contract exists to
# tell us how to redact properly. Conservative: if a value looks like
# any of these shapes, refuse the export and ask the operator to
# declare a contract or skip mounts.
_VALUE_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"AKIA[0-9A-Z]{12,}"),  # AWS access key IDs
    re.compile(r"ASIA[0-9A-Z]{12,}"),  # AWS temp access key
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI / anthropic prefixed keys
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),  # anthropic
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),  # GitHub PAT
    re.compile(r"gho_[A-Za-z0-9]{36,}"),  # GitHub OAuth token
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),  # GitLab PAT
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"AIza[0-9A-Za-z_-]{35,}"),  # Google API key
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),  # PEM private keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),  # bearer tokens
    re.compile(
        r"(?i)(?:password|secret|token|api[_-]?key|access[_-]?key)\s*=\s*[^\s]{8,}"
    ),  # KEY=VALUE assignments inside command strings
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),  # URL userinfo (user:pass@host)
    # Round-6 additions — token-only userinfo and JWTs that the
    # round-5 patterns missed.
    re.compile(r"://[A-Za-z0-9._\-+/=]{16,}@"),  # URL token-only (https://TOKEN@host)
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    ),  # JWT (header.payload.signature, base64url, eyJ prefix)
]


def _scan_config_values_for_secret_patterns(config: dict[str, Any]) -> list[str]:
    """Return dotted paths of values in `config` whose stringified form
    matches a known credential-shaped pattern. Walks nested dicts/lists.

    Used only on the no-CONNECTION_ARGS-contract path where we can't
    rely on declared structure to know which fields are secret.
    """
    leaks: list[str] = []

    def _walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            for rx in _VALUE_SECRET_PATTERNS:
                if rx.search(value):
                    leaks.append(path or "<root>")
                    return
        elif isinstance(value, dict):
            for k, v in value.items():
                here = f"{path}.{k}" if path else (k if isinstance(k, str) else f"[{k}]")
                _walk(v, here)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                _walk(item, f"{path}[{i}]")

    _walk(config, "")
    return sorted(set(leaks))


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
