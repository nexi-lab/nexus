"""Unified auth service for OAuth, stored secrets, and native providers.

This service keeps the user-facing auth surface consistent across backends
without forcing static secrets through OAuth-specific storage semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.unified_auth import (
    AuthResolution,
    AuthStatus,
    AuthSummary,
    CredentialKind,
    SecretCredentialRecord,
)
from nexus.fs._credentials import discover_credentials
from nexus.security.secret_file import write_secret_file

logger = logging.getLogger(__name__)
_UNSET = object()

if TYPE_CHECKING:
    from nexus.bricks.auth.profile import AuthProfileStore
    from nexus.contracts.types import OperationContext

_DEFAULT_STORE_PATH = Path("~/.nexus/auth/credentials.json").expanduser()
_STORE_VERSION = 1
_DOC_MIME_TYPE = "application/vnd.google-apps.document"
_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"

_SECRET_SERVICE_SPECS: dict[str, dict[str, Any]] = {
    "s3": {
        "backend_types": {"path_s3"},
        "required_fields": ("access_key_id", "secret_access_key"),
        "optional_fields": ("session_token", "region_name", "credentials_path"),
        "supports_native": True,
        "action_hint": "Run `nexus auth connect s3 secret` or configure `aws configure`.",
    },
    "gcs": {
        "backend_types": {"path_gcs"},
        "required_fields": (),
        "optional_fields": ("credentials_path", "access_token", "project_id"),
        "supports_native": True,
        "action_hint": (
            "Run `nexus auth connect gcs secret` or `gcloud auth application-default login`."
        ),
    },
}

_OAUTH_PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "gws": ("google",),
    "google-drive": ("google-drive", "google"),
    "google-calendar": ("google-calendar", "google"),
    "gmail": ("gmail", "google"),
    "slack": ("slack",),
    "x": ("x", "twitter"),
}

_GOOGLE_OAUTH_SERVICES = frozenset(
    service for service, providers in _OAUTH_PROVIDER_ALIASES.items() if "google" in providers
)

_GWS_TARGETS: tuple[str, ...] = ("drive", "docs", "sheets", "gmail", "calendar", "chat")
_SERVICE_TARGET_ALIASES: dict[str, tuple[str, ...]] = {
    "gws": _GWS_TARGETS,
    "google-drive": ("drive",),
    "gmail": ("gmail",),
    "google-calendar": ("calendar",),
}
_GWS_TARGET_PROBES: dict[str, list[str]] = {
    "drive": [
        "gws",
        "drive",
        "files",
        "list",
        "--params",
        json.dumps({"pageSize": 1}),
        "--format",
        "json",
    ],
    "docs": [
        "gws",
        "drive",
        "files",
        "list",
        "--params",
        json.dumps(
            {
                "q": f'mimeType = "{_DOC_MIME_TYPE}"',
                "pageSize": 1,
                "fields": "files(id,name,mimeType,modifiedTime,size,quotaBytesUsed)",
            }
        ),
        "--format",
        "json",
    ],
    "sheets": [
        "gws",
        "drive",
        "files",
        "list",
        "--params",
        json.dumps({"q": f'mimeType = "{_SHEET_MIME_TYPE}"', "pageSize": 1}),
        "--format",
        "json",
    ],
    "gmail": [
        "gws",
        "gmail",
        "users",
        "getProfile",
        "--params",
        '{"userId":"me"}',
        "--format",
        "json",
    ],
    "calendar": ["gws", "calendar", "calendarList", "list", "--format", "json"],
    "chat": ["gws", "chat", "spaces", "list", "--format", "json"],
}

_GWS_TARGET_SCOPE_MAP: dict[str, tuple[str, ...]] = {
    "drive": (
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
    ),
    "docs": (
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/documents.readonly",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
    ),
    "sheets": (
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
    ),
    "gmail": (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    ),
    "calendar": (
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
    ),
    "chat": (
        "https://www.googleapis.com/auth/chat.spaces",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
    ),
}


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class FileSecretCredentialStore:
    """Small file-backed secret store with restrictive filesystem perms."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser() if path is not None else _DEFAULT_STORE_PATH

    @property
    def path(self) -> Path:
        return self._path

    def list(self) -> list[SecretCredentialRecord]:
        payload = self._read_raw()
        entries = payload.get("entries", {})
        result: list[SecretCredentialRecord] = []
        for service, entry in entries.items():
            result.append(
                SecretCredentialRecord(
                    service=service,
                    kind=CredentialKind(entry["kind"]),
                    data=dict(entry.get("data", {})),
                    created_at=entry.get("created_at"),
                    updated_at=entry.get("updated_at"),
                )
            )
        return sorted(result, key=lambda item: item.service)

    def get(self, service: str) -> SecretCredentialRecord | None:
        payload = self._read_raw()
        entry = payload.get("entries", {}).get(service)
        if entry is None:
            return None
        return SecretCredentialRecord(
            service=service,
            kind=CredentialKind(entry["kind"]),
            data=dict(entry.get("data", {})),
            created_at=entry.get("created_at"),
            updated_at=entry.get("updated_at"),
        )

    def upsert(self, service: str, kind: CredentialKind, data: dict[str, str]) -> None:
        payload = self._read_raw()
        entries = payload.setdefault("entries", {})
        existing = entries.get(service, {})
        created_at = existing.get("created_at", _utcnow_iso())
        entries[service] = {
            "kind": kind.value,
            "data": data,
            "created_at": created_at,
            "updated_at": _utcnow_iso(),
        }
        self._write_raw(payload)

    def delete(self, service: str) -> bool:
        payload = self._read_raw()
        entries = payload.get("entries", {})
        if service not in entries:
            return False
        del entries[service]
        self._write_raw(payload)
        return True

    def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": _STORE_VERSION, "entries": {}}
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError:
            logger.warning("Secret credential store is corrupt: %s", self._path)
            return {"version": _STORE_VERSION, "entries": {}}
        if not isinstance(data, dict):
            return {"version": _STORE_VERSION, "entries": {}}
        data.setdefault("version", _STORE_VERSION)
        data.setdefault("entries", {})
        return data

    def _write_raw(self, payload: dict[str, Any]) -> None:
        write_secret_file(self._path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


class UnifiedAuthService:
    """Shared auth UX layer across OAuth and non-OAuth backends."""

    def __init__(
        self,
        oauth_service: OAuthCredentialService | None = None,
        *,
        secret_store: FileSecretCredentialStore | None = None,
        profile_store: "AuthProfileStore | None" = None,
    ) -> None:
        self._oauth_service = oauth_service
        self._secret_store = secret_store or FileSecretCredentialStore()
        # Unified auth-profile store (Phase 1, #3738). When provided, reads
        # go through this store first (typically a DualReadAuthProfileStore
        # that wraps the new SqliteAuthProfileStore + old store adapter).
        # Writes and CLI commands still use the old stores until Phase 4.
        self._profile_store = profile_store

    @property
    def secret_store_path(self) -> Path:
        return self._secret_store.path

    def resolve_backend_config(
        self,
        backend_type: str,
        config: dict[str, Any],
    ) -> AuthResolution:
        """Resolve secret/native credentials for storage backends."""
        service = self._service_for_backend_type(backend_type)
        if service is None:
            return AuthResolution(
                service=backend_type,
                status=AuthStatus.UNKNOWN,
                source="unsupported",
                resolved_config=dict(config),
            )

        explicit = self._explicit_secret_fields(service, config)
        if explicit:
            return AuthResolution(
                service=service,
                status=AuthStatus.AUTHED,
                source="explicit_config",
                resolved_config=dict(config),
                message="Using credentials provided directly in backend config.",
            )

        record = self._secret_store.get(service)
        if record is not None:
            if record.kind == CredentialKind.NATIVE:
                native = self._detect_native(service)
                if native is not None:
                    return AuthResolution(
                        service=service,
                        status=AuthStatus.AUTHED,
                        source="native",
                        resolved_config=dict(config),
                        message=native["message"],
                    )
                spec = _SECRET_SERVICE_SPECS[service]
                return AuthResolution(
                    service=service,
                    status=AuthStatus.NO_AUTH,
                    source="missing",
                    resolved_config=dict(config),
                    message=spec["action_hint"],
                )
            resolved = self._merge_secret_data(dict(config), record.data)
            return AuthResolution(
                service=service,
                status=AuthStatus.AUTHED,
                source=f"stored:{record.kind.value}",
                resolved_config=resolved,
                message=f"Using stored {record.kind.value} credentials for {service}.",
            )

        native = self._detect_native(service)
        if native is not None:
            return AuthResolution(
                service=service,
                status=AuthStatus.AUTHED,
                source="native",
                resolved_config=dict(config),
                message=native["message"],
            )

        spec = _SECRET_SERVICE_SPECS[service]
        return AuthResolution(
            service=service,
            status=AuthStatus.NO_AUTH,
            source="missing",
            resolved_config=dict(config),
            message=spec["action_hint"],
        )

    async def list_summaries(
        self,
        context: "OperationContext | None" = None,
    ) -> list[AuthSummary]:
        """Return redacted auth summaries for all known services."""
        summaries: list[AuthSummary] = []
        seen_services: set[str] = set()
        google_target_checks: dict[str, dict[str, Any]] | None = None

        for service in sorted(_SECRET_SERVICE_SPECS):
            seen_services.add(service)
            record = self._secret_store.get(service)
            if record is not None:
                if record.kind == CredentialKind.NATIVE:
                    native = self._detect_native(service)
                    if native is not None:
                        summaries.append(
                            AuthSummary(
                                service=service,
                                kind=CredentialKind.NATIVE,
                                status=AuthStatus.AUTHED,
                                source="native",
                                message=native["message"],
                                details={k: v for k, v in native.items() if k != "message"},
                            )
                        )
                    else:
                        summaries.append(
                            AuthSummary(
                                service=service,
                                kind=CredentialKind.NATIVE,
                                status=AuthStatus.NO_AUTH,
                                source="missing",
                                message=_SECRET_SERVICE_SPECS[service]["action_hint"],
                            )
                        )
                    continue
                summaries.append(
                    AuthSummary(
                        service=service,
                        kind=record.kind,
                        status=AuthStatus.AUTHED,
                        source=f"stored:{record.kind.value}",
                        message=f"Stored {record.kind.value} credential available.",
                        details=self._redact_secret_record(record),
                    )
                )
                continue

            native = self._detect_native(service)
            if native is not None:
                summaries.append(
                    AuthSummary(
                        service=service,
                        kind=CredentialKind.NATIVE,
                        status=AuthStatus.AUTHED,
                        source="native",
                        message=native["message"],
                        details={k: v for k, v in native.items() if k != "message"},
                    )
                )
            else:
                summaries.append(
                    AuthSummary(
                        service=service,
                        kind=CredentialKind.SECRET,
                        status=AuthStatus.NO_AUTH,
                        source="missing",
                        message=_SECRET_SERVICE_SPECS[service]["action_hint"],
                    )
                )

        if self._oauth_service is not None:
            oauth_creds = await self._oauth_service.list_credentials(context=context)
            # Phase 3 (#3740): lookup gws-cli-synced profiles from the unified
            # profile store instead of probing the CLI directly. The adapter
            # framework keeps these profiles fresh via AdapterRegistry.
            cached_native: dict[str, str] | None | object = _UNSET
            for service, providers in _OAUTH_PROVIDER_ALIASES.items():
                seen_services.add(service)
                matching = [cred for cred in oauth_creds if cred.get("provider") in providers]
                if service in _GOOGLE_OAUTH_SERVICES:
                    if cached_native is _UNSET:
                        cached_native = self._gws_native_from_profile_store()
                    native = cached_native if isinstance(cached_native, dict) else None
                else:
                    native = None
                if native is not None and google_target_checks is None:
                    google_target_checks = await self._probe_google_workspace_targets(
                        _GWS_TARGETS,
                        native=native,
                        user_email=native.get("email"),
                    )
                if matching:
                    expired = all(bool(cred.get("is_expired")) for cred in matching)
                    if expired and native is not None:
                        target_summary = self._google_target_summary(service, google_target_checks)
                        summaries.append(
                            AuthSummary(
                                service=service,
                                kind=CredentialKind.NATIVE,
                                status=target_summary["status"],
                                source=native["source"],
                                message=target_summary["message"],
                                details={
                                    **{k: v for k, v in native.items() if k not in {"message"}},
                                    "stored_oauth_status": AuthStatus.EXPIRED.value,
                                    "providers": sorted(
                                        {str(cred.get("provider")) for cred in matching}
                                    ),
                                    "accounts": len(matching),
                                    **target_summary["details"],
                                },
                            )
                        )
                        continue

                    if not expired and service in _SERVICE_TARGET_ALIASES:
                        target_summary = await self._google_target_summary_for_stored_oauth(
                            service,
                            str(matching[0].get("provider", "")),
                            str(matching[0].get("user_email", "")),
                        )
                        summaries.append(
                            AuthSummary(
                                service=service,
                                kind=CredentialKind.OAUTH,
                                status=target_summary["status"],
                                source="oauth",
                                message=target_summary["message"],
                                details=target_summary["details"],
                            )
                        )
                        continue

                    status = AuthStatus.EXPIRED if expired else AuthStatus.AUTHED
                    summaries.append(
                        AuthSummary(
                            service=service,
                            kind=CredentialKind.OAUTH,
                            status=status,
                            source="oauth",
                            message=(
                                "OAuth credentials are present but expired."
                                if expired
                                else "OAuth credentials available."
                            ),
                            details={
                                "providers": sorted(
                                    {str(cred.get("provider")) for cred in matching}
                                ),
                                "accounts": len(matching),
                            },
                        )
                    )
                elif native is not None:
                    target_summary = self._google_target_summary(service, google_target_checks)
                    summaries.append(
                        AuthSummary(
                            service=service,
                            kind=CredentialKind.NATIVE,
                            status=target_summary["status"],
                            source=native["source"],
                            message=target_summary["message"],
                            details={
                                **{k: v for k, v in native.items() if k != "message"},
                                **target_summary["details"],
                            },
                        )
                    )
                else:
                    summaries.append(
                        AuthSummary(
                            service=service,
                            kind=CredentialKind.OAUTH,
                            status=AuthStatus.NO_AUTH,
                            source="missing",
                            message=f"Run `nexus auth connect {service} oauth`.",
                        )
                    )

        return sorted(summaries, key=lambda item: item.service)

    async def get_connector_auth_state(
        self,
        service_name: str | None,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, str | None]:
        """Return auth status/source for connector discovery surfaces."""
        if service_name is None:
            return {"auth_status": "unknown", "auth_source": None}

        for summary in await self.list_summaries(context=context):
            if summary.service == service_name:
                return {
                    "auth_status": summary.status.value,
                    "auth_source": summary.source,
                }
        return {"auth_status": "unknown", "auth_source": None}

    async def test_service(
        self,
        service: str,
        *,
        user_email: str | None = None,
        target: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Verify auth readiness for a service."""
        if service in _SECRET_SERVICE_SPECS:
            record = self._secret_store.get(service)
            if record is not None:
                validation = self._validate_secret_record(record)
                if validation is None:
                    return {
                        "success": True,
                        "service": service,
                        "source": f"stored:{record.kind.value}",
                        "message": f"Stored {record.kind.value} credentials look valid.",
                    }
                return {
                    "success": False,
                    "service": service,
                    "source": f"stored:{record.kind.value}",
                    "message": validation,
                }

            native = self._detect_native(service)
            if native is not None:
                return {
                    "success": True,
                    "service": service,
                    "source": "native",
                    "message": native["message"],
                }

            return {
                "success": False,
                "service": service,
                "source": "missing",
                "message": _SECRET_SERVICE_SPECS[service]["action_hint"],
            }

        if self._oauth_service is None:
            return {
                "success": False,
                "service": service,
                "source": "oauth",
                "message": "OAuth service is not available in this environment.",
            }

        providers = _OAUTH_PROVIDER_ALIASES.get(service)
        if not providers:
            return {
                "success": False,
                "service": service,
                "source": "unknown",
                "message": f"Unknown auth service '{service}'.",
            }

        oauth_creds = await self._oauth_service.list_credentials(context=context)
        matches = [cred for cred in oauth_creds if cred.get("provider") in providers]
        if user_email is not None:
            matches = [cred for cred in matches if cred.get("user_email") == user_email]
        desired_targets = self._google_targets_for_service(service, target=target)
        # Phase 3 (#3740): read from profile store instead of probing gws CLI.
        native = self._oauth_native_from_profile_store(service, user_email=user_email)
        if not matches:
            if native is not None:
                if desired_targets:
                    return await self._google_target_test_result(
                        service,
                        desired_targets,
                        source=native["source"],
                        native=native,
                        user_email=user_email or native.get("email"),
                    )
                return {
                    "success": True,
                    "service": service,
                    "source": native["source"],
                    "message": native["message"],
                }
            return {
                "success": False,
                "service": service,
                "source": "oauth",
                "message": f"Run `nexus auth connect {service} oauth`.",
            }

        candidate = matches[0]
        if desired_targets and not bool(candidate.get("is_expired")):
            return await self._google_target_test_result_for_stored_oauth(
                service,
                desired_targets,
                provider=str(candidate["provider"]),
                user_email=str(candidate["user_email"]),
                default_source="oauth",
            )

        if bool(candidate.get("is_expired")) and native is not None:
            if desired_targets:
                result = await self._google_target_test_result(
                    service,
                    desired_targets,
                    source=native["source"],
                    native=native,
                    user_email=user_email or native.get("email"),
                )
                result["message"] = "Stored OAuth credential is expired; " + str(
                    result.get("message", "using local native CLI auth.")
                )
                result["stored_oauth_status"] = AuthStatus.EXPIRED.value
                return result
            return {
                "success": True,
                "service": service,
                "source": native["source"],
                "message": "Stored OAuth credential is expired; using local native CLI auth.",
            }
        result = await self._oauth_service.test_credential(
            provider=str(candidate["provider"]),
            user_email=str(candidate["user_email"]),
            context=context,
        )
        valid = bool(result.get("success")) if "success" in result else bool(result.get("valid"))
        if not valid:
            message = str(
                result.get("message") or result.get("error") or "OAuth credential test failed."
            )
            return {
                "success": False,
                "service": service,
                "source": "oauth",
                "message": message,
            }
        result["service"] = service
        result.setdefault("source", "oauth")
        if desired_targets:
            return await self._google_target_test_result_for_stored_oauth(
                service,
                desired_targets,
                provider=str(candidate["provider"]),
                user_email=str(candidate["user_email"]),
                default_source=str(result.get("source", "oauth")),
            )
        return result

    def connect_secret(self, service: str, values: dict[str, str]) -> SecretCredentialRecord:
        """Store static secret data for a service."""
        if service not in _SECRET_SERVICE_SPECS:
            raise ValueError(f"Unsupported secret-backed service '{service}'")
        filtered = {k: v for k, v in values.items() if v}
        self._secret_store.upsert(service, CredentialKind.SECRET, filtered)
        record = self._secret_store.get(service)
        if record is None:
            raise RuntimeError(f"Failed to store credentials for {service}")
        return record

    def connect_native(self, service: str) -> SecretCredentialRecord:
        """Store an explicit native-provider preference marker."""
        if service not in _SECRET_SERVICE_SPECS:
            raise ValueError(f"Unsupported native-backed service '{service}'")
        self._secret_store.upsert(service, CredentialKind.NATIVE, {})
        record = self._secret_store.get(service)
        if record is None:
            raise RuntimeError(f"Failed to store native marker for {service}")
        return record

    def disconnect(self, service: str) -> bool:
        """Remove a stored credential or native marker."""
        return self._secret_store.delete(service)

    def _service_for_backend_type(self, backend_type: str) -> str | None:
        for service, spec in _SECRET_SERVICE_SPECS.items():
            if backend_type in spec["backend_types"]:
                return service
        return None

    def _explicit_secret_fields(self, service: str, config: dict[str, Any]) -> dict[str, Any]:
        spec = _SECRET_SERVICE_SPECS[service]
        fields = (*spec["required_fields"], *spec["optional_fields"])
        return {field: config[field] for field in fields if config.get(field)}

    def _merge_secret_data(self, config: dict[str, Any], data: dict[str, str]) -> dict[str, Any]:
        resolved = dict(config)
        for key, value in data.items():
            resolved.setdefault(key, value)
        return resolved

    def _detect_native(self, service: str) -> dict[str, str] | None:
        if not _SECRET_SERVICE_SPECS[service]["supports_native"]:
            return None
        try:
            details = discover_credentials(service)
        except Exception:
            return None
        message = f"Native provider chain available via {details.get('source', 'native')}."
        return {**{k: str(v) for k, v in details.items()}, "message": message}

    def _gws_native_from_profile_store(self) -> dict[str, str] | None:
        """Return gws-cli-synced profile info from the unified profile store.

        Phase 3 (#3740): replaces _detect_google_workspace_cli_native(), which
        was a hardcoded subprocess probe. The adapter framework
        (GwsCliSyncAdapter + AdapterRegistry) now keeps google profiles fresh
        in the store — we just read them here.
        """
        if self._profile_store is None:
            return None

        try:
            gws_profiles = [
                p
                for p in self._profile_store.list(provider="google")
                if p.backend == "external-cli" and p.backend_key.startswith("gws-cli/")
            ]
        except Exception:
            return None

        if not gws_profiles:
            return None

        email = gws_profiles[0].account_identifier
        return {
            "source": "native:gws_cli",
            "email": email,
            "message": f"Local gws CLI profile available for {email}.",
        }

    def _oauth_native_from_profile_store(
        self,
        service: str,
        *,
        user_email: str | None = None,
    ) -> dict[str, str] | None:
        """OAuth-specific wrapper: filter by service + optional user email."""
        if service not in _GOOGLE_OAUTH_SERVICES:
            return None
        native = self._gws_native_from_profile_store()
        if native is None:
            return None
        if user_email and user_email != native.get("email"):
            return None
        return native

    def _google_targets_for_service(
        self,
        service: str,
        *,
        target: str | None = None,
    ) -> tuple[str, ...]:
        if service not in _SERVICE_TARGET_ALIASES:
            return ()
        if target is None:
            return _SERVICE_TARGET_ALIASES[service]
        normalized = target.strip().lower()
        if normalized not in _GWS_TARGETS:
            raise ValueError(
                f"Unknown Google Workspace target '{target}'. Choose from: {', '.join(_GWS_TARGETS)}."
            )
        allowed = _SERVICE_TARGET_ALIASES[service]
        if normalized not in allowed:
            raise ValueError(f"Target '{normalized}' is not valid for service '{service}'.")
        return (normalized,)

    async def _get_stored_oauth_credential(self, provider: str, user_email: str) -> Any | None:
        if self._oauth_service is None:
            return None
        token_manager_getter = getattr(self._oauth_service, "_get_token_manager", None)
        if token_manager_getter is None:
            return None
        try:
            token_manager = token_manager_getter()
        except Exception:
            return None
        if token_manager is None:
            return None
        getter = getattr(token_manager, "get_credential", None)
        if getter is None:
            return None
        try:
            return await getter(provider, user_email, ROOT_ZONE_ID)
        except Exception:
            return None

    async def _probe_google_workspace_targets(
        self,
        targets: tuple[str, ...],
        *,
        native: dict[str, str] | None = None,
        user_email: str | None = None,
        access_token: str | None = None,
        source: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        probe_source = source or (native["source"] if native is not None else "oauth")
        probe_email = user_email or (native.get("email") if native is not None else None)
        if access_token is None and native is None:
            return {
                target: {
                    "target": target,
                    "success": False,
                    "source": probe_source,
                    "message": "No Google auth is available for target verification.",
                    "reason": "missing_google_auth",
                }
                for target in targets
            }

        async def _probe_single(target: str) -> tuple[str, dict[str, Any]]:
            args = _GWS_TARGET_PROBES[target]
            proc: asyncio.subprocess.Process | None = None
            try:
                env = {**os.environ}
                if access_token:
                    env["GWS_ACCESS_TOKEN"] = access_token
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=3)
            except BaseException as exc:
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                error_msg = str(exc) or f"{target} probe timed out or was cancelled."
                return target, {
                    "target": target,
                    "success": False,
                    "source": probe_source,
                    "message": error_msg,
                    "reason": "probe_error",
                }

            stdout = stdout_bytes.decode()
            stderr = stderr_bytes.decode()
            combined = f"{stdout}\n{stderr}".lower()
            if proc.returncode == 0:
                return target, {
                    "target": target,
                    "success": True,
                    "source": probe_source,
                    "message": (
                        f"{target} target is ready via stored OAuth."
                        if access_token
                        else f"{target} target is ready via local gws CLI."
                    ),
                }

            if "insufficient authentication scopes" in combined:
                message = (
                    f"{target} requires additional Google OAuth scopes. Re-run the Google "
                    f"Workspace OAuth connect flow for {probe_email or 'your account'} and approve the requested access."
                )
                reason = "missing_scopes"
            elif "auth_expired" in combined or "expired" in combined:
                message = (
                    f"{target} auth expired. Re-run the Google Workspace OAuth connect flow "
                    f"for {probe_email or 'your account'}."
                )
                reason = "expired"
            else:
                summary = (stderr or stdout).strip() or f"{target} probe failed."
                message = summary.splitlines()[0]
                reason = "probe_failed"

            return target, {
                "target": target,
                "success": False,
                "source": probe_source,
                "message": message,
                "reason": reason,
            }

        results = await asyncio.gather(*[_probe_single(t) for t in targets])
        return dict(results)

    async def _google_target_test_result_for_stored_oauth(
        self,
        service: str,
        targets: tuple[str, ...],
        *,
        provider: str,
        user_email: str,
        default_source: str,
    ) -> dict[str, Any]:
        credential = await self._get_stored_oauth_credential(provider, user_email)
        if credential is None:
            return {
                "success": False,
                "service": service,
                "source": default_source,
                "message": "Stored OAuth credential could not be loaded for target verification.",
            }

        scopes = set(credential.scopes or ())
        missing_scope_targets = [
            target
            for target in targets
            if not any(scope in scopes for scope in _GWS_TARGET_SCOPE_MAP[target])
        ]
        if missing_scope_targets:
            checks = [
                {
                    "target": target,
                    "success": target not in missing_scope_targets,
                    "source": "oauth",
                    "message": (
                        f"{target} target is ready via stored OAuth."
                        if target not in missing_scope_targets
                        else (
                            f"{target} requires additional Google OAuth scopes. Re-run "
                            f"`nexus-fs auth connect gws oauth --user-email {user_email}` "
                            "and approve the requested access."
                        )
                    ),
                    "reason": None if target not in missing_scope_targets else "missing_scopes",
                }
                for target in targets
            ]
            return {
                "success": False,
                "service": service,
                "source": "oauth",
                "message": "; ".join(
                    f"{target}: missing required Google OAuth scope"
                    for target in missing_scope_targets
                ),
                "checks": checks,
                "stored_oauth_status": AuthStatus.AUTHED.value,
            }

        return await self._google_target_test_result(
            service,
            targets,
            source="oauth",
            user_email=user_email,
            access_token=credential.access_token,
        )

    async def _google_target_summary_for_stored_oauth(
        self,
        service: str,
        provider: str,
        user_email: str,
    ) -> dict[str, Any]:
        result = await self._google_target_test_result_for_stored_oauth(
            service,
            self._google_targets_for_service(service),
            provider=provider,
            user_email=user_email,
            default_source="oauth",
        )
        checks = result.get("checks", [])
        if not isinstance(checks, list):
            checks = []
        failed = [check for check in checks if not check.get("success")]
        status = AuthStatus.AUTHED if result.get("success") else AuthStatus.ERROR
        if service == "gws":
            if failed:
                failed_text = ", ".join(
                    f"{check.get('target')} ({check.get('reason', 'failed')})" for check in failed
                )
                message = f"Stored Google OAuth is available, but some targets are not ready: {failed_text}."
            else:
                ready = ", ".join(
                    str(check.get("target")) for check in checks if check.get("success")
                )
                message = f"Google Workspace targets ready via stored OAuth: {ready}."
        else:
            message = str(result.get("message", "OAuth credential available."))
        return {
            "status": status,
            "message": message,
            "details": {
                "target_checks": {
                    str(check.get("target")): {
                        "success": bool(check.get("success")),
                        "reason": check.get("reason"),
                        "message": check.get("message"),
                    }
                    for check in checks
                },
            },
        }

    def _google_target_summary(
        self,
        service: str,
        checks: dict[str, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        if checks is None:
            return {
                "status": AuthStatus.AUTHED,
                "message": "Local gws CLI profile available.",
                "details": {},
            }

        targets = _SERVICE_TARGET_ALIASES.get(service, ())
        if not targets:
            return {
                "status": AuthStatus.AUTHED,
                "message": "Local gws CLI profile available.",
                "details": {},
            }

        selected = [checks[target] for target in targets if target in checks]
        ready = [item["target"] for item in selected if item.get("success")]
        failed = [item for item in selected if not item.get("success")]

        if service == "gws":
            if failed:
                failed_summary = ", ".join(
                    f"{item['target']} ({item.get('reason', 'failed')})" for item in failed
                )
                message = (
                    "Base Google identity available via local gws CLI, but some targets are not "
                    f"ready: {failed_summary}."
                )
                status = AuthStatus.ERROR
            else:
                message = (
                    "Google Workspace targets ready via local gws CLI: " + ", ".join(ready) + "."
                )
                status = AuthStatus.AUTHED
        else:
            item = selected[0] if selected else None
            if item is None:
                status = AuthStatus.UNKNOWN
                message = "Target readiness could not be determined."
            elif item.get("success"):
                status = AuthStatus.AUTHED
                message = str(item["message"])
            else:
                status = AuthStatus.ERROR
                message = str(item["message"])

        return {
            "status": status,
            "message": message,
            "details": {
                "target_checks": {
                    item["target"]: {
                        "success": bool(item.get("success")),
                        "reason": item.get("reason"),
                        "message": item.get("message"),
                    }
                    for item in selected
                }
            },
        }

    async def _google_target_test_result(
        self,
        service: str,
        targets: tuple[str, ...],
        *,
        source: str,
        native: dict[str, str] | None = None,
        user_email: str | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        checks = await self._probe_google_workspace_targets(
            targets,
            native=native,
            user_email=user_email,
            access_token=access_token,
            source=source,
        )
        selected = [checks[target] for target in targets]
        failed = [item for item in selected if not item.get("success")]
        ready = [item["target"] for item in selected if item.get("success")]

        if failed:
            summary = "; ".join(f"{item['target']}: {item['message']}" for item in failed)
            return {
                "success": False,
                "service": service,
                "source": source,
                "message": summary,
                "checks": selected,
            }

        target_text = ", ".join(ready)
        return {
            "success": True,
            "service": service,
            "source": source,
            "message": f"Targets ready: {target_text}.",
            "checks": selected,
        }

    def _validate_secret_record(self, record: SecretCredentialRecord) -> str | None:
        if record.kind == CredentialKind.NATIVE:
            native = self._detect_native(record.service)
            if native is None:
                return cast(str, _SECRET_SERVICE_SPECS[record.service]["action_hint"])
            return None

        spec = _SECRET_SERVICE_SPECS[record.service]
        required_fields: tuple[str, ...] = spec["required_fields"]
        if record.service == "gcs" and not any(
            record.data.get(field) for field in ("credentials_path", "access_token")
        ):
            return "GCS stored credentials require `credentials_path` or `access_token`."
        missing = [field for field in required_fields if not record.data.get(field)]
        if missing:
            return f"Missing required fields: {', '.join(sorted(missing))}."
        path_value = record.data.get("credentials_path")
        if path_value and not Path(path_value).expanduser().exists():
            return f"Credential file not found: {path_value}"
        return None

    def _redact_secret_record(self, record: SecretCredentialRecord) -> dict[str, Any]:
        details = {
            "stored_at": record.updated_at,
            "fields": sorted(record.data),
        }
        path_value = record.data.get("credentials_path")
        if path_value:
            details["credentials_path"] = path_value
        return details

    @staticmethod
    def store_help_fields(service: str) -> dict[str, Any]:
        """Expose field metadata for CLI prompts/tests."""
        if service not in _SECRET_SERVICE_SPECS:
            raise ValueError(f"Unsupported service '{service}'")
        spec = _SECRET_SERVICE_SPECS[service]
        return {
            "required_fields": spec["required_fields"],
            "optional_fields": spec["optional_fields"],
            "supports_native": spec["supports_native"],
        }

    @staticmethod
    def supported_services() -> dict[str, dict[str, Any]]:
        """Expose service spec metadata for CLI and tests."""
        return {
            service: {
                "kind": "secret" if service in _SECRET_SERVICE_SPECS else "oauth",
                **spec,
            }
            for service, spec in _SECRET_SERVICE_SPECS.items()
        }

    @staticmethod
    def oauth_services() -> tuple[str, ...]:
        return tuple(sorted(_OAUTH_PROVIDER_ALIASES))
