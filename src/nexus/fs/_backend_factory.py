"""Backend creation logic for the nexus-fs slim package.

Extracted from ``__init__.py`` — handles backend instantiation from
parsed MountSpec objects, connector discovery, and OAuth user inference.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def create_backend(spec: Any) -> Any:
    """Create a storage backend from a parsed MountSpec.

    Discovers credentials automatically and instantiates the
    appropriate backend class.

    Raises:
        CloudCredentialError: If required credentials are missing.
        ImportError: If the backend's optional dependency is not installed.
        BackendNotFoundError: If the backend resource doesn't exist.
    """
    if spec.scheme == "s3":
        try:
            from nexus.backends.storage.path_s3 import PathS3Backend
        except ImportError:
            raise ImportError(
                "boto3 is required for S3 backends. Install with: pip install nexus-fs[s3]"
            ) from None

        # Phase 2: ensure external-CLI sync has run (populates auth list).
        # S3 credential resolution always uses boto3's native provider chain
        # (which handles SSO/STS refresh, instance metadata, etc.). The profile
        # store is for discovery/listing in `auth list`, not for credential
        # injection at mount time.
        from nexus.fs._external_sync_boot import ensure_external_sync

        ensure_external_sync()

        from nexus.fs._credentials import discover_credentials

        discover_credentials(spec.scheme)
        return PathS3Backend(
            bucket_name=spec.authority,
            prefix=spec.path.lstrip("/") if spec.path else "",
        )

    elif spec.scheme == "gcs":
        from nexus.fs._credentials import discover_credentials

        discover_credentials(spec.scheme)
        try:
            from nexus.backends.storage.cas_gcs import CASGCSBackend
        except ImportError:
            raise ImportError(
                "google-cloud-storage is required for GCS backends. "
                "Install with: pip install nexus-fs[gcs]"
            ) from None
        from nexus.fs._uri import derive_bucket

        return CASGCSBackend(bucket_name=derive_bucket(spec), project_id=spec.authority)

    elif spec.scheme in ("local", "cas-local"):
        # Passthrough by default — when a user mounts ``local://./data``
        # they expect files to land at ``./data/<virtual_path>`` and be
        # directly visible on disk.  Content-addressed storage is an
        # implementation detail of the nexus server's CAS profile, not a
        # user-facing default for a filesystem mount.  ``cas-local://``
        # is the explicit opt-in for dedup / hash-named blob storage.
        #
        # URI reconstruction: ``parse_uri`` splits ``local:///abs/path``
        # into ``authority="abs"`` + ``path="path"`` because urllib's
        # netloc empty-case handler takes the first path segment as a
        # stand-in authority.  Concatenating those back drops the
        # leading ``/``, giving a cwd-relative path (e.g.
        # ``$(pwd)/abspath``) instead of the intended absolute one.
        # Rebuild from the original URI — everything after the scheme
        # prefix is the filesystem path as the user typed it.
        from pathlib import Path as _Path

        prefix = f"{spec.scheme}://"
        raw_path = spec.uri[len(prefix) :] if spec.uri.startswith(prefix) else ""
        if not raw_path:
            # Fallback for callers that constructed a MountSpec directly.
            raw_path = spec.authority + (spec.path or "")
        root = _Path(raw_path).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if spec.scheme == "cas-local":
            from nexus.backends.storage.cas_local import CASLocalBackend

            return CASLocalBackend(root_path=root)
        from nexus.backends.storage.path_local import PathLocalBackend

        return PathLocalBackend(root_path=root)

    else:
        # Fall through to the connector registry for any other scheme.
        return _create_connector_backend(spec)


def _create_connector_backend(spec: Any) -> Any:
    """Create a backend from the connector registry.

    Attempts to import connector modules for the scheme and look up a
    matching connector in the ConnectorRegistry. The lookup convention:
    ``{scheme}_{authority}`` first, then ``{scheme}_connector`` as fallback.
    """
    scheme = spec.scheme
    authority = spec.authority

    # Ensure manifest placeholders are registered FIRST so that any
    # `@register_connector("name")` call inside _discover_connector_module
    # hits the placeholder-binding path and preserves manifest-sourced
    # metadata (description, category, runtime_deps, service_name).
    # Without this, a direct URI-scheme import takes the external-plugin
    # branch of register() with empty defaults and the entry has no
    # runtime_deps — mount skips the MissingDependencyError check.
    from nexus.backends import _register_optional_backends

    _register_optional_backends()

    # If any manifest entry references this scheme, Phase 2 of
    # _register_optional_backends already attempted the import — the
    # placeholder (or bound class) is authoritative. Skip
    # _discover_connector_module entirely so a transitive ImportError
    # from re-importing the module doesn't mask the canonical
    # MissingDependencyError / placeholder RuntimeError.
    #
    # For schemes NOT in the manifest (external plugins packaged as
    # nexus.backends.connectors.<x>.connector), preserve the original
    # behavior: _discover_connector_module propagates real import
    # errors so operators see the actual root cause rather than a
    # generic "connector not found".
    from nexus.backends._manifest import CONNECTOR_MANIFEST

    # Scheme is "known to the manifest" when it maps to a connector
    # module OR to a registry name stripped of the ``_connector`` suffix.
    # The latter catches alias entries like ``gcalendar_connector`` whose
    # module_path points at ``...connectors.calendar.connector`` —
    # without it a ``gcalendar://`` mount would skip past the placeholder
    # lookup and crash in _discover_connector_module on a missing
    # ``nexus.backends.connectors.gcalendar`` package.
    manifest_schemes: set[str] = set()
    for entry in CONNECTOR_MANIFEST:
        if entry.module_path.endswith(".connector"):
            manifest_schemes.add(entry.module_path.rsplit(".connector", 1)[0].split(".")[-1])
        if entry.name.endswith("_connector"):
            manifest_schemes.add(entry.name.removesuffix("_connector"))
    if scheme not in manifest_schemes:
        _discover_connector_module(scheme)

    from nexus.backends.base.registry import ConnectorRegistry

    connector_name = f"{scheme}_{authority}" if authority else scheme
    fallback_name = f"{scheme}_connector"

    # Resolve via get_info() so we can see BOTH bound classes and
    # manifest placeholders. get() hides placeholders behind a KeyError,
    # which would collapse "missing Python dep" into "connector not
    # found" — the exact ambiguity Issue #3830 set out to eliminate.
    info = None
    selected_name: str | None = None
    for candidate in [
        connector_name,
        f"gws_{authority}" if scheme == "gws" else None,
        fallback_name,
    ]:
        if candidate is None:
            continue
        try:
            info = ConnectorRegistry.get_info(candidate)
            selected_name = candidate
            break
        except KeyError:
            continue

    if info is None:
        from nexus.contracts.exceptions import NexusURIError

        available = ConnectorRegistry.list_available()
        raise NexusURIError(
            spec.uri,
            f"No backend or connector found for scheme '{scheme}://'. "
            f"Built-in: s3://, gcs://, local://. "
            f"Registered connectors: {', '.join(available) if available else 'none'}",
        )

    # Placeholder-first dep check: mirror BackendFactory.create() so the
    # URI path produces the same diagnostics as programmatic mounts.
    from nexus.backends.base.runtime_deps import check_runtime_deps
    from nexus.contracts.exceptions import MissingDependencyError

    missing = check_runtime_deps(info.runtime_deps)
    if missing:
        raise MissingDependencyError(backend=selected_name or scheme, missing=missing)

    if info.connector_class is None:
        raise RuntimeError(
            f"Connector '{selected_name or scheme}' is declared in the "
            f"manifest but its module failed to import. Check logs for "
            f"the original ImportError."
        )

    return _instantiate_connector_backend(info.connector_class, info=info, scheme=scheme)


def _default_token_manager_db() -> str:
    """Return the default TokenManager database path/URL for slim fs mounts."""
    from nexus.lib.env import get_database_url

    db_url = get_database_url()
    if db_url:
        return db_url

    from nexus.fs._paths import token_manager_db

    db_path = token_manager_db()
    return str(db_path)


def _infer_connector_user_email(
    *,
    scheme: str,
    info: Any | None,
) -> str | None:
    """Best-effort user identity for OAuth-backed slim connector mounts.

    Priority:
    1. ``NEXUS_FS_USER_EMAIL`` explicit override
    2. the only stored OAuth credential email for the service's provider(s)
    """
    explicit = os.getenv("NEXUS_FS_USER_EMAIL")
    if explicit:
        return explicit

    service_name = getattr(info, "service_name", None) or scheme

    # These imports are from nexus.bricks which is excluded from the slim wheel.
    # Gracefully degrade when running outside the monorepo.
    try:
        from nexus.fs._oauth_support import get_token_manager
    except Exception:
        return None

    try:
        oauth_module = importlib.import_module("nexus.bricks.auth.oauth.credential_service")
        unified_module = importlib.import_module("nexus.bricks.auth.unified_service")
    except (ModuleNotFoundError, ImportError):
        # Expected when running from the slim wheel (nexus.bricks is excluded)
        logger.debug("nexus.bricks.auth not available — skipping OAuth user inference")
        return None

    oauth_provider_aliases = getattr(unified_module, "_OAUTH_PROVIDER_ALIASES", {})
    providers = oauth_provider_aliases.get(service_name)
    if not providers:
        return None

    import asyncio

    oauth_service = oauth_module.OAuthCredentialService(token_manager=get_token_manager())
    coro = oauth_service.list_credentials()
    try:
        creds = asyncio.run(coro)
    except Exception:
        coro.close()  # prevent "coroutine never awaited" RuntimeWarning
        return None

    emails = sorted(
        {
            str(cred.get("user_email"))
            for cred in creds
            if cred.get("provider") in providers and cred.get("user_email")
        }
    )
    if len(emails) == 1:
        return emails[0]
    if "google" in providers:
        return _infer_google_workspace_cli_email()
    return None


def _infer_google_workspace_cli_email() -> str | None:
    """Best-effort Google account detection from the local gws CLI auth state."""
    if shutil.which("gws") is None:
        return None

    try:
        result = subprocess.run(
            [
                "gws",
                "gmail",
                "users",
                "getProfile",
                "--params",
                '{"userId":"me"}',
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    stdout = result.stdout.strip()
    if not stdout:
        return None

    try:
        import json

        start = stdout.find("{")
        payload = stdout[start:] if start >= 0 else stdout
        data = json.loads(payload)
    except Exception:
        return None

    email = str(data.get("emailAddress") or "").strip()
    return email or None


def _instantiate_connector_backend(connector_cls: Any, *, info: Any | None, scheme: str) -> Any:
    """Instantiate connector with the same auth defaults the mount service injects."""
    init_sig = inspect.signature(connector_cls.__init__)
    params = init_sig.parameters
    kwargs: dict[str, Any] = {}

    connection_args = getattr(info, "connection_args", {}) if info is not None else {}
    if "token_manager_db" in params or "token_manager_db" in connection_args:
        kwargs["token_manager_db"] = _default_token_manager_db()

    if "user_email" in params or "user_email" in connection_args:
        user_email = _infer_connector_user_email(scheme=scheme, info=info)
        if user_email:
            kwargs["user_email"] = user_email

    # Pass the process-singleton encryption key so the connector's TokenManager
    # uses the same key as exchange_auth_code() — without this, tokens stored by
    # exchange_auth_code are unreadable by the connector's _get_drive_service.
    if "encryption_key" in params:
        from nexus.fs._oauth_support import get_oauth_encryption_key

        kwargs["encryption_key"] = get_oauth_encryption_key()

    return connector_cls(**kwargs)


def _discover_connector_module(scheme: str) -> None:
    """Try to import the connector module for a given scheme.

    Connector modules register themselves via @register_connector when
    imported. This is a no-op if the module doesn't exist or has already
    been imported.

    Distinguishes between:
    - ``ModuleNotFoundError`` for the connector module itself → expected, skip
    - ``ModuleNotFoundError`` for a transitive dependency → re-raise (real bug)
    - ``ImportError`` → re-raise (module exists but broken)
    """
    module_paths = [
        f"nexus.backends.connectors.{scheme}.connector",
        f"nexus.backends.connectors.{scheme}",
    ]
    for mod_path in module_paths:
        try:
            importlib.import_module(mod_path)
            return
        except ModuleNotFoundError as exc:
            # Treat "the connector module or one of its ancestor packages
            # under nexus.backends.connectors is absent" as expected — the
            # discovery loop will fall back to registry lookup and a clean
            # NexusURIError. Any other missing name (a transitive dep like
            # ``googleapiclient`` inside a connector that DID resolve on
            # disk) must re-raise so the operator sees the real culprit.
            if exc.name is not None and (
                exc.name == mod_path
                or (
                    mod_path.startswith(exc.name + ".")
                    and exc.name.startswith("nexus.backends.connectors.")
                )
            ):
                continue
            raise
        except ImportError:
            # Module exists but failed to import (bug in connector) — re-raise
            raise
