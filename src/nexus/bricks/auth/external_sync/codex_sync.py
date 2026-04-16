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
            base / "auth.json",  # Current Codex CLI layout (OPENAI_API_KEY + tokens)
            base / "credentials.json",  # Multi-profile layout for older installs
            base / "config.json",  # Additional fallback
        ]

    def parse_file(self, path: Path, content: str) -> list[SyncedProfile]:
        """Parse a codex credential file.

        ``auth.json`` is a single-account file with ``OPENAI_API_KEY`` +
        ``tokens`` (chatgpt mode) or ``OPENAI_API_KEY`` alone (api-key mode).
        ``credentials.json`` / ``config.json`` may contain a profile-keyed
        dict of ``{name: {api_key|token|tokens, ...}}``.
        """
        if not content.strip():
            return []

        data = json.loads(content)
        if not isinstance(data, dict):
            return []

        # Single-account auth.json shape.
        if path.name == "auth.json" or self._looks_single_account(data):
            identifier = self._identify_single_account(data)
            if identifier is None:
                return []
            return [
                SyncedProfile(
                    provider="codex",
                    account_identifier=identifier,
                    backend_key=f"codex/{identifier}",
                    source="codex",
                )
            ]

        # Multi-profile shape: {profile_name: {api_key|token|tokens}}
        profiles: list[SyncedProfile] = []
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if not self._entry_has_credential(entry):
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

    @staticmethod
    def _looks_single_account(data: dict) -> bool:
        """True if the top-level dict itself carries credential fields."""
        return bool(
            data.get("OPENAI_API_KEY")
            or data.get("api_key")
            or data.get("token")
            or isinstance(data.get("tokens"), dict)
        )

    @staticmethod
    def _identify_single_account(data: dict) -> str | None:
        """Return a stable identifier for a single-account credential file.

        Prefers ``tokens.account_id``; falls back to ``auth_mode`` so
        "chatgpt" / "apikey" distinctly route. Returns None if we can't
        name the account at all.
        """
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            account_id = str(tokens.get("account_id", "")).strip()
            if account_id:
                return account_id
        mode = str(data.get("auth_mode", "")).strip()
        if mode:
            return mode
        if data.get("OPENAI_API_KEY") or data.get("api_key"):
            return "default"
        return None

    @staticmethod
    def _entry_has_credential(entry: dict) -> bool:
        return bool(
            entry.get("api_key") or entry.get("token") or isinstance(entry.get("tokens"), dict)
        )

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Re-read credential file and extract one profile's credential.

        Tries single-account ``auth.json`` shape first (OPENAI_API_KEY +
        tokens), then multi-profile dicts. Returns the first credential
        material found matching ``profile_name``.
        """
        _, profile_name = backend_key.split("/", 1)

        for path in self.paths():
            try:
                content = path.read_text(encoding="utf-8")
                data = json.loads(content)
            except (OSError, json.JSONDecodeError):
                continue

            if not isinstance(data, dict):
                continue

            # Single-account shape.
            if path.name == "auth.json" or self._looks_single_account(data):
                identifier = self._identify_single_account(data)
                if identifier != profile_name:
                    continue
                return self._credential_from_single(data)

            # Multi-profile shape.
            entry = data.get(profile_name)
            if not isinstance(entry, dict):
                continue
            cred = self._credential_from_entry(entry)
            if cred is not None:
                return cred

        raise CredentialResolutionError(
            "external-cli",
            backend_key,
            f"Codex profile '{profile_name}' not found in config files",
        )

    @staticmethod
    def _credential_from_single(data: dict) -> ResolvedCredential:
        """Build a ResolvedCredential from a single-account auth.json."""
        # chatgpt mode: tokens.access_token is the bearer token.
        tokens = data.get("tokens")
        if isinstance(tokens, dict) and tokens.get("access_token"):
            return ResolvedCredential(
                kind="bearer_token",
                access_token=str(tokens["access_token"]),
                metadata={
                    k: str(v) for k, v in tokens.items() if k != "access_token" and v is not None
                },
            )
        # api-key mode: OPENAI_API_KEY at top level.
        api_key = data.get("OPENAI_API_KEY") or data.get("api_key")
        if api_key:
            return ResolvedCredential(
                kind="api_key",
                api_key=str(api_key),
                metadata={"auth_mode": str(data.get("auth_mode", "apikey"))},
            )
        # Last resort: top-level token.
        token = data.get("token")
        if token:
            return ResolvedCredential(kind="bearer_token", access_token=str(token))
        raise CredentialResolutionError(
            "external-cli", "codex", "auth.json has no credential material"
        )

    @staticmethod
    def _credential_from_entry(entry: dict) -> ResolvedCredential | None:
        """Build a ResolvedCredential from a multi-profile entry."""
        if entry.get("api_key"):
            return ResolvedCredential(
                kind="api_key",
                api_key=str(entry["api_key"]),
                metadata={k: str(v) for k, v in entry.items() if k != "api_key"},
            )
        if entry.get("token"):
            return ResolvedCredential(
                kind="bearer_token",
                access_token=str(entry["token"]),
                metadata={k: str(v) for k, v in entry.items() if k != "token"},
            )
        tokens = entry.get("tokens")
        if isinstance(tokens, dict) and tokens.get("access_token"):
            return ResolvedCredential(
                kind="bearer_token",
                access_token=str(tokens["access_token"]),
            )
        return None
