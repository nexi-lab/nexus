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
    from nexus.fs._credentials import discover_credentials

    # Discover credentials (raises CloudCredentialError if missing)
    discover_credentials(spec.scheme)

    if spec.scheme == "s3":
        try:
            from nexus.backends.storage.path_s3 import PathS3Backend
        except ImportError:
            raise ImportError(
                "boto3 is required for S3 backends. Install with: pip install nexus-fs[s3]"
            ) from None
        return PathS3Backend(
            bucket_name=spec.authority,
            prefix=spec.path.lstrip("/") if spec.path else "",
        )

    elif spec.scheme == "gcs":
        try:
            from nexus.backends.storage.cas_gcs import CASGCSBackend
        except ImportError:
            raise ImportError(
                "google-cloud-storage is required for GCS backends. "
                "Install with: pip install nexus-fs[gcs]"
            ) from None
        # GCS: gcs://project/bucket → authority=project, path=/bucket
        bucket = spec.path.strip("/").split("/")[0] if spec.path else spec.authority
        return CASGCSBackend(bucket_name=bucket, project_id=spec.authority)

    elif spec.scheme == "local":
        from pathlib import Path as _Path

        from nexus.backends.storage.cas_local import CASLocalBackend

        root = _Path(spec.authority + (spec.path or "")).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return CASLocalBackend(root_path=root)

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

    _discover_connector_module(scheme)

    from nexus.backends.base.registry import ConnectorRegistry

    connector_name = f"{scheme}_{authority}" if authority else scheme
    fallback_name = f"{scheme}_connector"

    connector_cls = None
    selected_name: str | None = None
    for candidate in [
        connector_name,
        f"gws_{authority}" if scheme == "gws" else None,
        fallback_name,
    ]:
        if candidate is None:
            continue
        try:
            connector_cls = ConnectorRegistry.get(candidate)
            selected_name = candidate
            break
        except KeyError:
            continue

    if connector_cls is None:
        from nexus.contracts.exceptions import NexusURIError

        available = ConnectorRegistry.list_available()
        raise NexusURIError(
            spec.uri,
            f"No backend or connector found for scheme '{scheme}://'. "
            f"Built-in: s3://, gcs://, local://. "
            f"Registered connectors: {', '.join(available) if available else 'none'}",
        )

    info = ConnectorRegistry.get_info(selected_name) if selected_name is not None else None
    return _instantiate_connector_backend(connector_cls, info=info, scheme=scheme)


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

    oauth_service = oauth_module.OAuthCredentialService(token_manager=get_token_manager())
    try:
        import asyncio

        creds = asyncio.run(oauth_service.list_credentials())
    except Exception:
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

    return connector_cls(**kwargs)


def _discover_connector_module(scheme: str) -> None:
    """Try to import the connector module for a given scheme.

    Connector modules register themselves via @register_connector when
    imported. This is a no-op if the module doesn't exist or has already
    been imported.

    Distinguishes between ``ModuleNotFoundError`` (expected — module doesn't
    exist) and ``ImportError`` (unexpected — module exists but has a bug).
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
            # Only treat as "module doesn't exist" if the missing module
            # is the one we tried to import. If a transitive dependency
            # inside the connector is missing, that's a real bug.
            if exc.name is not None and exc.name == mod_path:
                continue
            # Transitive dependency missing — treat as a real import failure
            logger.warning(
                "Connector module %s has a missing dependency: %s",
                mod_path,
                exc,
            )
            continue
        except ImportError as exc:
            # Unexpected: the module exists but failed to import (bug)
            logger.warning(
                "Connector module %s exists but failed to import: %s",
                mod_path,
                exc,
            )
            continue
