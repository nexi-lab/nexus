"""GitHub CLI sync adapter — dual-mode subprocess + file fallback.

Composes both SubprocessAdapter and FileAdapter strategies internally.
Primary: ``gh auth status --show-token``. Fallback: parse
``~/.config/gh/hosts.yml`` when the binary isn't on PATH.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import yaml

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)

logger = logging.getLogger(__name__)


class GhCliSyncAdapter(ExternalCliSyncAdapter):
    """Discovers GitHub CLI credentials via subprocess or hosts.yml fallback."""

    adapter_name = "gh-cli"
    sync_ttl_seconds: float = 300.0  # subprocess = expensive

    def _config_dir(self) -> Path:
        return Path(os.environ.get("GH_CONFIG_DIR", "~/.config/gh")).expanduser()

    def _hosts_path(self) -> Path:
        return self._config_dir() / "hosts.yml"

    def _has_binary(self) -> bool:
        return shutil.which("gh") is not None

    async def detect(self) -> bool:
        if self._has_binary():
            return True
        try:
            p = self._hosts_path()
            return p.exists() and p.is_file()
        except OSError:
            return False

    async def sync(self) -> SyncResult:
        if self._has_binary():
            return await self._sync_subprocess()
        return self._sync_file()

    async def _sync_subprocess(self) -> SyncResult:
        """Run gh auth status --show-token and parse output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "auth",
                "status",
                "--show-token",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except TimeoutError:
            return SyncResult(adapter_name=self.adapter_name, error="gh: timeout after 5s")
        except FileNotFoundError:
            return SyncResult(adapter_name=self.adapter_name, error="gh: binary not found")

        # gh auth status prints to stderr in older versions, stdout in newer
        output = stdout_bytes.decode("utf-8", errors="replace")
        if not output.strip():
            output = stderr_bytes.decode("utf-8", errors="replace")

        if not output.strip():
            return SyncResult(
                adapter_name=self.adapter_name,
                error="gh auth status returned empty output",
            )

        try:
            profiles = self.parse_status_output(output)
        except Exception as exc:
            return SyncResult(adapter_name=self.adapter_name, error=f"gh: parse error: {exc}")

        return SyncResult(adapter_name=self.adapter_name, profiles=profiles)

    def _sync_file(self) -> SyncResult:
        """Parse hosts.yml as fallback when gh binary is not available."""
        hosts_path = self._hosts_path()
        try:
            content = hosts_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"gh: {hosts_path} not found and binary not on PATH",
            )
        except OSError as exc:
            return SyncResult(adapter_name=self.adapter_name, error=f"gh: {exc}")

        if not content.strip():
            return SyncResult(adapter_name=self.adapter_name, error="gh: hosts.yml is empty")

        try:
            profiles = self.parse_hosts_file(content)
        except Exception as exc:
            return SyncResult(adapter_name=self.adapter_name, error=f"gh: parse error: {exc}")

        return SyncResult(adapter_name=self.adapter_name, profiles=profiles)

    def parse_hosts_file(self, content: str) -> list[SyncedProfile]:
        """Parse ~/.config/gh/hosts.yml into profiles.

        Supports v2.40 (flat) and v2.50 (nested users) formats.
        """
        if not content.strip():
            return []

        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return []

        profiles: list[SyncedProfile] = []
        for host, host_data in data.items():
            if not isinstance(host_data, dict):
                continue

            # v2.50: nested users dict
            users = host_data.get("users")
            if isinstance(users, dict):
                for username, user_data in users.items():
                    if isinstance(user_data, dict) and user_data.get("oauth_token"):
                        profiles.append(
                            SyncedProfile(
                                provider="github",
                                account_identifier=username,
                                backend_key=f"gh-cli/{host}/{username}",
                                source="gh-cli",
                            )
                        )
            # v2.40: flat oauth_token + user
            elif host_data.get("oauth_token") and host_data.get("user"):
                profiles.append(
                    SyncedProfile(
                        provider="github",
                        account_identifier=host_data["user"],
                        backend_key=f"gh-cli/{host}/{host_data['user']}",
                        source="gh-cli",
                    )
                )

        return profiles

    def parse_status_output(self, output: str) -> list[SyncedProfile]:
        """Parse ``gh auth status --show-token`` text output."""
        profiles: list[SyncedProfile] = []
        current_host: str | None = None

        for line in output.splitlines():
            stripped = line.strip()
            # Host line: no leading whitespace, ends with domain
            if not line.startswith(" ") and not line.startswith("\t") and stripped:
                current_host = stripped.rstrip(":")
                continue

            if current_host is None:
                continue

            # Match: "Logged in to <host> as <user>" or "account <user>"
            m = re.search(r"Logged in to \S+ (?:as|account) (\S+)", stripped)
            if m:
                username = m.group(1).strip("()")
                profiles.append(
                    SyncedProfile(
                        provider="github",
                        account_identifier=username,
                        backend_key=f"gh-cli/{current_host}/{username}",
                        source="gh-cli",
                    )
                )

        return profiles

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Resolve token from hosts.yml (sync-safe file read)."""
        parts = backend_key.split("/", 2)
        if len(parts) < 3:
            raise CredentialResolutionError(
                "external-cli",
                backend_key,
                f"expected 'gh-cli/host/user', got {backend_key!r}",
            )
        _, host, username = parts

        hosts_path = self._hosts_path()
        try:
            content = hosts_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
        except Exception as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, f"Cannot read hosts.yml: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise CredentialResolutionError(
                "external-cli", backend_key, "hosts.yml is not a valid YAML mapping"
            )

        host_data = data.get(host)
        if not isinstance(host_data, dict):
            raise CredentialResolutionError(
                "external-cli", backend_key, f"Host '{host}' not found in hosts.yml"
            )

        # v2.50 nested
        users = host_data.get("users")
        if isinstance(users, dict):
            user_data = users.get(username)
            if isinstance(user_data, dict) and user_data.get("oauth_token"):
                return ResolvedCredential(
                    kind="bearer_token",
                    access_token=user_data["oauth_token"],
                )

        # v2.40 flat
        if host_data.get("user") == username and host_data.get("oauth_token"):
            return ResolvedCredential(
                kind="bearer_token",
                access_token=host_data["oauth_token"],
            )

        raise CredentialResolutionError(
            "external-cli",
            backend_key,
            f"User '{username}' not found for host '{host}' in hosts.yml",
        )
