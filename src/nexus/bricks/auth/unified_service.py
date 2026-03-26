"""Unified auth service for OAuth, stored secrets, and native providers.

This service keeps the user-facing auth surface consistent across backends
without forcing static secrets through OAuth-specific storage semantics.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService
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

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

_DEFAULT_STORE_PATH = Path("~/.nexus/auth/credentials.json").expanduser()
_STORE_VERSION = 1

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
    ) -> None:
        self._oauth_service = oauth_service
        self._secret_store = secret_store or FileSecretCredentialStore()

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

        for service in sorted(_SECRET_SERVICE_SPECS):
            seen_services.add(service)
            record = self._secret_store.get(service)
            if record is not None:
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
            for service, providers in _OAUTH_PROVIDER_ALIASES.items():
                seen_services.add(service)
                matching = [cred for cred in oauth_creds if cred.get("provider") in providers]
                native = self._detect_oauth_native(service)
                if matching:
                    expired = all(bool(cred.get("is_expired")) for cred in matching)
                    if expired and native is not None:
                        summaries.append(
                            AuthSummary(
                                service=service,
                                kind=CredentialKind.NATIVE,
                                status=AuthStatus.AUTHED,
                                source=native["source"],
                                message=(
                                    "Stored OAuth credentials are expired; using local native CLI auth."
                                ),
                                details={
                                    **{k: v for k, v in native.items() if k not in {"message"}},
                                    "stored_oauth_status": AuthStatus.EXPIRED.value,
                                    "providers": sorted(
                                        {str(cred.get("provider")) for cred in matching}
                                    ),
                                    "accounts": len(matching),
                                },
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
                    summaries.append(
                        AuthSummary(
                            service=service,
                            kind=CredentialKind.NATIVE,
                            status=AuthStatus.AUTHED,
                            source=native["source"],
                            message=native["message"],
                            details={k: v for k, v in native.items() if k != "message"},
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
        if not matches:
            native = self._detect_oauth_native(service, user_email=user_email)
            if native is not None:
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
        if bool(candidate.get("is_expired")):
            native = self._detect_oauth_native(service, user_email=user_email)
            if native is not None:
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
        result["service"] = service
        result.setdefault("source", "oauth")
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

    def _detect_oauth_native(
        self,
        service: str,
        *,
        user_email: str | None = None,
    ) -> dict[str, str] | None:
        if service not in _GOOGLE_OAUTH_SERVICES:
            return None
        return self._detect_google_workspace_cli_native(user_email=user_email)

    def _detect_google_workspace_cli_native(
        self,
        *,
        user_email: str | None = None,
    ) -> dict[str, str] | None:
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
            start = stdout.find("{")
            payload = stdout[start:] if start >= 0 else stdout
            data = json.loads(payload)
        except Exception:
            return None

        email = str(data.get("emailAddress") or "").strip()
        if not email:
            return None
        if user_email and user_email != email:
            return None

        return {
            "source": "native:gws_cli",
            "email": email,
            "message": f"Local gws CLI profile available for {email}.",
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
