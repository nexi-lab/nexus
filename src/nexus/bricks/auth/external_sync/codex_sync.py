"""Codex sync adapter — discovers credentials from ~/.codex/ config files.

FileAdapter subclass. Reads ~/.codex/credentials.json (primary) and
~/.codex/config.json (fallback).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import SyncedProfile
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter


class CodexSyncAdapter(FileAdapter):
    """Discovers Codex credentials from config files."""

    adapter_name = "codex"

    def _config_dir(self) -> Path:
        return Path(os.environ.get("CODEX_CONFIG_DIR", "~/.codex")).expanduser()

    def paths(self) -> list[Path]:
        base = self._config_dir()
        return [
            base / "credentials.json",
            base / "config.json",
        ]

    def parse_file(self, _path: Path, content: str) -> list[SyncedProfile]:
        if not content.strip():
            return []

        data = json.loads(content)
        if not isinstance(data, dict):
            return []

        profiles: list[SyncedProfile] = []
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if not (entry.get("api_key") or entry.get("token")):
                continue
            profiles.append(
                SyncedProfile(
                    provider="codex",
                    account_identifier=name,
                    backend_key=f"codex/{name}",
                    source="codex",
                )
            )

        return profiles

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Re-read credential file and extract one profile's credential."""
        _, profile_name = backend_key.split("/", 1)

        for path in self.paths():
            try:
                content = path.read_text(encoding="utf-8")
                data = json.loads(content)
            except (OSError, json.JSONDecodeError):
                continue

            if not isinstance(data, dict):
                continue

            entry = data.get(profile_name)
            if not isinstance(entry, dict):
                continue

            if entry.get("api_key"):
                return ResolvedCredential(
                    kind="api_key",
                    api_key=entry["api_key"],
                    metadata={k: str(v) for k, v in entry.items() if k != "api_key"},
                )
            if entry.get("token"):
                return ResolvedCredential(
                    kind="bearer_token",
                    access_token=entry["token"],
                    metadata={k: str(v) for k, v in entry.items() if k != "token"},
                )

        raise CredentialResolutionError(
            "external-cli",
            backend_key,
            f"Codex profile '{profile_name}' not found in config files",
        )
