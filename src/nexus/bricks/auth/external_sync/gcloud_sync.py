"""Gcloud sync adapter — discovers credentials from ADC + properties files.

FileAdapter subclass. Reads ~/.config/gcloud/application_default_credentials.json
and ~/.config/gcloud/properties. Does NOT hit the gcloud metadata server —
the ADC file is the source of truth (offline-safe).
"""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import SyncedProfile
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter


class GcloudSyncAdapter(FileAdapter):
    """Discovers gcloud credentials from ADC + active config."""

    adapter_name = "gcloud"

    def _config_dir(self) -> Path:
        return Path(os.environ.get("CLOUDSDK_CONFIG", "~/.config/gcloud")).expanduser()

    def paths(self) -> list[Path]:
        """Return ADC + the active configuration's properties file.

        Real gcloud uses a per-configuration layout (``configurations/config_<name>``)
        with ``active_config`` naming the current one. We also return the legacy
        flat ``properties`` path for older installs.
        """
        base = self._config_dir()
        paths: list[Path] = [base / "application_default_credentials.json"]

        # Prefer the active configuration's properties file.
        active_config_marker = base / "active_config"
        try:
            if active_config_marker.is_file():
                active = active_config_marker.read_text(encoding="utf-8").strip()
                if active:
                    paths.append(base / "configurations" / f"config_{active}")
        except OSError:
            pass

        # Legacy flat layout — still read it if the new layout didn't land a path.
        paths.append(base / "properties")
        return paths

    def parse_file(self, _path: Path, content: str) -> list[SyncedProfile]:
        if not content.strip():
            return []

        # Dispatch by content shape: JSON starts with '{', INI has '[section]'.
        stripped = content.lstrip()
        if stripped.startswith("{"):
            return self._parse_adc(content)
        if stripped.startswith("["):
            return self._parse_properties(content)
        return []

    def _parse_adc(self, content: str) -> list[SyncedProfile]:
        """Parse the ADC JSON.

        service_account: produces a profile keyed on client_email.
        authorized_user: returns [] — the ADC carries no user identity, so
            account discovery comes from the ``properties`` file instead.
            This avoids the phantom ``gcloud/unknown`` profile that older
            code emitted. _resolve_impl still reads ADC to get the actual
            credential once properties has named the account.
        """
        data = json.loads(content)
        cred_type = data.get("type", "")

        if cred_type == "service_account":
            email = data.get("client_email", "")
            if not email:
                return []
            return [
                SyncedProfile(
                    provider="gcs",
                    account_identifier=email,
                    backend_key=f"gcloud/{email}",
                    source="gcloud",
                )
            ]

        # authorized_user and any unknown types: no profile from ADC alone.
        return []

    def _parse_properties(self, content: str) -> list[SyncedProfile]:
        parser = configparser.ConfigParser()
        parser.read_string(content)

        if not parser.has_option("core", "account"):
            return []

        account = parser.get("core", "account").strip()
        if not account:
            return []

        return [
            SyncedProfile(
                provider="gcs",
                account_identifier=account,
                backend_key=f"gcloud/{account}",
                source="gcloud",
            )
        ]

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Shared resolve logic — re-read ADC JSON and extract credential."""
        adc_path = self._config_dir() / "application_default_credentials.json"

        try:
            content = adc_path.read_text(encoding="utf-8")
            data = json.loads(content)
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, f"Cannot read ADC: {exc}"
            ) from exc

        cred_type = data.get("type", "")

        if cred_type == "service_account":
            return ResolvedCredential(
                kind="api_key",
                api_key=data.get("private_key", ""),
                metadata={"client_email": data.get("client_email", "")},
            )

        if cred_type == "authorized_user":
            return ResolvedCredential(
                kind="bearer_token",
                access_token=None,
                metadata={
                    "client_id": data.get("client_id", ""),
                    "client_secret": data.get("client_secret", ""),
                    "refresh_token": data.get("refresh_token", ""),
                },
            )

        raise CredentialResolutionError(
            "external-cli", backend_key, f"Unknown ADC type: {cred_type!r}"
        )
