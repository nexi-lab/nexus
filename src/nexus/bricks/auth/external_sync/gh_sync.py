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
import subprocess
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
        """Run gh auth status and parse output.

        On non-zero exit (not logged in, keyring unavailable, enterprise
        quirks, …) fall back to parsing ``hosts.yml`` instead of silently
        reporting zero profiles. A successful-but-empty SyncResult would
        combine with the TTL-aware refresh gate to permanently suppress
        later profile discovery in a long-lived process; always surface
        the failure so the file-fallback path runs.
        """
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "auth",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except TimeoutError:
            # Kill the child so the runtime reaps it — otherwise a repeated
            # timeout path leaks zombies on every TTL-triggered refresh.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            # Fall back to hosts.yml rather than reporting empty success.
            return self._sync_file_with_reason("gh: timeout after 5s")
        except FileNotFoundError:
            return self._sync_file_with_reason("gh: binary not found")

        # gh auth status output split varies by version + stream targeting:
        #   - older gh: status lines on stderr, --show-token tokens on stdout
        #   - newer gh: status lines on stdout
        #   - keyring-backed installs: mixed (login info on stderr, errors stdout)
        # Parse the COMBINED stream so we don't miss accounts that landed on
        # the "wrong" side. The earlier "prefer stdout, fall back to stderr"
        # pattern dropped logins on mixed-stream installs.
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        combined = (stdout + "\n" + stderr).strip()

        # Non-zero exit: not logged in, keyring unavailable, etc. Surface the
        # failure via the file-fallback rather than reporting zero profiles.
        if proc.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"exit code {proc.returncode}"
            return self._sync_file_with_reason(f"gh auth status: {detail}")

        if not combined:
            return self._sync_file_with_reason("gh auth status returned empty output")

        try:
            profiles = self.parse_status_output(combined)
        except Exception as exc:
            return self._sync_file_with_reason(f"gh: parse error: {exc}")

        if not profiles:
            # Subprocess succeeded but parsed no accounts — the file might have
            # them (e.g. user hasn't run ``gh auth status`` recently). Try file.
            return self._sync_file_with_reason("gh auth status parsed 0 profiles")

        return SyncResult(adapter_name=self.adapter_name, profiles=profiles)

    def _sync_file_with_reason(self, reason: str) -> SyncResult:
        """Try file-based sync; if that also fails, attach the subprocess reason."""
        result = self._sync_file()
        if result.profiles:
            # Success via file — ignore subprocess issue.
            return result
        # Both failed — combine error messages for diagnostics.
        combined = reason
        if result.error:
            combined = f"{reason} | file: {result.error}"
        return SyncResult(adapter_name=self.adapter_name, error=combined)

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
                                # Host-qualified to disambiguate same username
                                # across github.com vs enterprise hosts. Without
                                # this, registry profile_id (provider/account)
                                # collides and one host overwrites the other.
                                account_identifier=f"{host}/{username}",
                                backend_key=f"gh-cli/{host}/{username}",
                                source="gh-cli",
                            )
                        )
            # v2.40: flat oauth_token + user
            elif host_data.get("oauth_token") and host_data.get("user"):
                profiles.append(
                    SyncedProfile(
                        provider="github",
                        account_identifier=f"{host}/{host_data['user']}",
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
                        # Host-qualified — see parse_hosts_file note.
                        account_identifier=f"{current_host}/{username}",
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
        """Resolve token for a gh-cli profile.

        Dual-mode: when the ``gh`` binary is available, prefer
        ``gh auth token -h <host> -u <user>`` — this is the only way to
        retrieve the token when it's stored in the OS keyring (default
        on macOS and increasingly common on Linux with libsecret).

        Falls back to parsing ``hosts.yml`` when the binary is missing
        or the subprocess call fails — useful in CI where only the config
        file is present.
        """
        parts = backend_key.split("/", 2)
        if len(parts) < 3:
            raise CredentialResolutionError(
                "external-cli",
                backend_key,
                f"expected 'gh-cli/host/user', got {backend_key!r}",
            )
        _, host, username = parts

        # Subprocess path first (keyring-safe).
        binary_path = shutil.which("gh")
        if binary_path is not None:
            token = self._resolve_via_subprocess(binary_path, host, username)
            if token:
                return ResolvedCredential(kind="bearer_token", access_token=token)

        # File fallback.
        return self._resolve_via_file(backend_key, host, username)

    def _resolve_via_subprocess(self, binary_path: str, host: str, username: str) -> str | None:
        """Call ``gh auth token -h <host> -u <user>`` and return its stdout.

        Returns None (not raises) on any failure — caller falls back to file.

        **Narrow -u retry:** only retry without ``-u`` when stderr clearly
        indicates an unsupported-flag / version-skew case (gh < 2.40 before
        per-user tokens existed). For every other failure (wrong user, keyring
        locked, auth drift, enterprise quirks), retrying without ``-u`` would
        silently return the host's active-account token — a different user's
        credential. Fall through to None instead so the file path can verify
        account membership or fail cleanly.
        """
        try:
            proc = subprocess.run(
                [binary_path, "auth", "token", "-h", host, "-u", username],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        if proc.returncode != 0:
            # Only retry without -u if gh doesn't understand the flag.
            # gh < 2.40 emits "unknown flag: --user" or similar; modern gh
            # with a wrong user emits "no oauth token found" or "user not found".
            stderr_lower = (proc.stderr or "").lower()
            looks_like_unknown_flag = any(
                needle in stderr_lower
                for needle in (
                    "unknown flag",
                    "unknown shorthand flag",
                    "flag needs an argument",
                    "invalid argument",
                )
            )
            if not looks_like_unknown_flag:
                # Any other failure: refuse to fall back to the active-account
                # token. Caller will try the file path next.
                return None
            try:
                proc = subprocess.run(
                    [binary_path, "auth", "token", "-h", host],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                return None
            if proc.returncode != 0:
                return None

        token = proc.stdout.strip()
        return token or None

    def _resolve_via_file(self, backend_key: str, host: str, username: str) -> ResolvedCredential:
        """Parse ~/.config/gh/hosts.yml for the token — works even without the binary."""
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
