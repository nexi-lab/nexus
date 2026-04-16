"""GWS CLI sync adapter — discovers Google Workspace accounts via gws binary.

SubprocessAdapter subclass. Runs ``gws auth status --format=json`` to discover
connected accounts. Falls back to the legacy ``gws gmail users getProfile``
command if ``auth status`` is not available.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import SyncedProfile, SyncResult
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter
from nexus.bricks.auth.profile import AuthProfileFailureReason


class GwsCliSyncAdapter(SubprocessAdapter):
    """Discovers Google Workspace CLI credentials via subprocess."""

    adapter_name = "gws-cli"
    binary_name = "gws"

    FIX_HINTS: dict[AuthProfileFailureReason, str] = {
        AuthProfileFailureReason.UPSTREAM_CLI_MISSING: (
            "Install the gws CLI and run: gws auth login"
        ),
        AuthProfileFailureReason.AUTH_PERMANENT: "Run: gws auth login",
        AuthProfileFailureReason.SCOPE_INSUFFICIENT: (
            "Run: gws auth login --scopes=<required_scopes>"
        ),
        AuthProfileFailureReason.SESSION_EXPIRED: "Run: gws auth login",
        AuthProfileFailureReason.TIMEOUT: "Check network connectivity to Google APIs",
        AuthProfileFailureReason.CLOCK_SKEW: "Sync system clock (e.g. ntpdate pool.ntp.org)",
    }

    def get_status_args(self) -> tuple[str, ...]:
        return ("auth", "status", "--format=json")

    async def sync(self) -> SyncResult:
        """Try ``auth status`` first; fall back to legacy getProfile command."""
        result = await super().sync()
        if result.error is None:
            return result

        # Fallback: legacy command (same as the deleted unified_service probe)
        return await self._sync_legacy_probe()

    async def _sync_legacy_probe(self) -> SyncResult:
        """Fallback: gws gmail users getProfile --format json."""
        binary_path = shutil.which(self.binary_name)
        if binary_path is None:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: binary not found on PATH",
            )

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                binary_path,
                "gmail",
                "users",
                "getProfile",
                "--params",
                '{"userId":"me"}',
                "--format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except TimeoutError:
            # Same zombie-avoidance pattern as SubprocessAdapter._run_once.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            return SyncResult(adapter_name=self.adapter_name, error="gws: timeout")
        except FileNotFoundError:
            return SyncResult(adapter_name=self.adapter_name, error="gws: binary not found")

        if proc.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            detail = stderr or f"exit code {proc.returncode}"
            return SyncResult(
                adapter_name=self.adapter_name, error=f"gws: legacy probe failed: {detail}"
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not stdout:
            return SyncResult(adapter_name=self.adapter_name, error="gws: empty output")

        try:
            start = stdout.find("{")
            payload = stdout[start:] if start >= 0 else stdout
            data = json.loads(payload)
            email = str(data.get("emailAddress", "")).strip()
        except Exception as exc:
            return SyncResult(adapter_name=self.adapter_name, error=f"gws: parse error: {exc}")

        if not email:
            return SyncResult(adapter_name=self.adapter_name, error="gws: no email in response")

        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=[
                SyncedProfile(
                    provider="google",
                    account_identifier=email,
                    backend_key=f"gws-cli/{email}",
                    source="gws-cli",
                )
            ],
        )

    def parse_output(self, stdout: str, _stderr: str) -> list[SyncedProfile]:
        """Parse ``gws auth status --format=json`` output.

        Real gws CLI prints non-JSON preamble lines like
        ``Using keyring backend: keyring`` before the JSON blob, so we
        strip everything before the first ``{``. The current surface is a
        single-account object with a ``user`` field; a future multi-account
        ``accounts: [...]`` schema is also supported.
        """
        if not stdout.strip():
            return []

        start = stdout.find("{")
        payload = stdout[start:] if start >= 0 else stdout
        data = json.loads(payload)

        if not isinstance(data, dict):
            return []

        profiles: list[SyncedProfile] = []

        # Future/multi-account shape: {"accounts": [{"email": "...", "active": true}]}
        #
        # Only emit the active account. Bare ``gws auth token`` returns the
        # active account's token regardless of which profile the caller
        # asked for — emitting non-active profiles here would create
        # resolvable-looking backend_keys that can't actually be resolved
        # without an account-switch primitive we don't control.
        accounts = data.get("accounts")
        if isinstance(accounts, list):
            for acct in accounts:
                if not isinstance(acct, dict) or not acct.get("active"):
                    continue
                email = str(acct.get("email", "")).strip()
                if email:
                    profiles.append(self._profile_for_email(email))
            if profiles:
                return profiles

        # Current single-account shape: {"user": "...", ...}
        user = str(data.get("user", "")).strip()
        if user:
            profiles.append(self._profile_for_email(user))

        return profiles

    @staticmethod
    def _profile_for_email(email: str) -> SyncedProfile:
        return SyncedProfile(
            provider="google",
            account_identifier=email,
            backend_key=f"gws-cli/{email}",
            source="gws-cli",
        )

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Resolve a token for the specific account encoded in ``backend_key``.

        **Cross-user safety:** ``gws auth token`` always returns the token
        for the CLI's currently active account, not a specified one. If the
        user has multiple gws accounts (or switches accounts between sync
        and resolve), bare ``gws auth token`` can return the wrong identity's
        token — real cross-user credential bleed.

        This implementation verifies the active account matches the requested
        one before returning. Fails closed on mismatch rather than returning
        a silently-wrong credential.
        """
        # Parse the email the caller asked for.
        parts = backend_key.split("/", 1)
        if len(parts) < 2:
            raise CredentialResolutionError(
                "external-cli", backend_key, "expected 'gws-cli/<email>' backend_key"
            )
        requested_email = parts[1].strip()

        binary_path = shutil.which(self.binary_name)
        if binary_path is None:
            raise CredentialResolutionError(
                "external-cli",
                backend_key,
                f"{self.binary_name}: binary not found on PATH",
            )

        # Verify the CLI's active account matches the requested one BEFORE
        # pulling a token — otherwise we'd hand back whichever account gws
        # happens to be logged into right now.
        active_email = self._get_active_account_email(binary_path)
        if active_email is None:
            raise CredentialResolutionError(
                "external-cli",
                backend_key,
                "gws: cannot determine active account (run: gws auth login)",
            )
        if active_email != requested_email:
            raise CredentialResolutionError(
                "external-cli",
                backend_key,
                f"gws active account is {active_email!r}, not {requested_email!r}. "
                f"Run: gws auth switch {requested_email} (if your gws CLI supports it) "
                f"or re-login as that account",
            )

        try:
            proc = subprocess.run(
                [binary_path, "auth", "token", "--format=json"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, "gws auth token: timeout"
            ) from exc

        if proc.returncode != 0:
            error_detail = proc.stderr.strip() or f"exit code {proc.returncode}"
            raise CredentialResolutionError(
                "external-cli", backend_key, f"gws auth token: {error_detail}"
            )

        try:
            # Strip non-JSON preamble (e.g., "Using keyring backend: keyring").
            start = proc.stdout.find("{")
            payload = proc.stdout[start:] if start >= 0 else proc.stdout
            data = json.loads(payload)
            access_token = str(data.get("access_token", "")).strip()
        except (json.JSONDecodeError, AttributeError) as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, f"gws auth token: parse error: {exc}"
            ) from exc

        if not access_token:
            raise CredentialResolutionError(
                "external-cli", backend_key, "gws auth token: empty access_token"
            )

        return ResolvedCredential(kind="bearer_token", access_token=access_token)

    @staticmethod
    def _get_active_account_email(binary_path: str) -> str | None:
        """Return the gws CLI's currently active account email, or None.

        Runs ``gws auth status --format=json`` and reads the ``user`` field
        (real gws shape as of April 2026). Safe for keyring preamble lines.
        """
        try:
            proc = subprocess.run(
                [binary_path, "auth", "status", "--format=json"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        if proc.returncode != 0:
            return None

        try:
            start = proc.stdout.find("{")
            if start < 0:
                return None
            data = json.loads(proc.stdout[start:])
        except (json.JSONDecodeError, AttributeError):
            return None

        if not isinstance(data, dict):
            return None

        # Current single-account shape.
        user = str(data.get("user", "")).strip()
        if user:
            return user

        # Future multi-account shape: accounts[active=true].email
        accounts = data.get("accounts")
        if isinstance(accounts, list):
            for acct in accounts:
                if isinstance(acct, dict) and acct.get("active"):
                    email = str(acct.get("email", "")).strip()
                    if email:
                        return email

        return None
