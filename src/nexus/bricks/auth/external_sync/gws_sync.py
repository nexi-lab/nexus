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
        if not stdout.strip():
            return []

        data = json.loads(stdout)
        accounts = data.get("accounts", [])
        if not isinstance(accounts, list):
            return []

        profiles: list[SyncedProfile] = []
        for acct in accounts:
            if not isinstance(acct, dict):
                continue
            email = acct.get("email", "").strip()
            if not email:
                continue
            profiles.append(
                SyncedProfile(
                    provider="google",
                    account_identifier=email,
                    backend_key=f"gws-cli/{email}",
                    source="gws-cli",
                )
            )

        return profiles

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Run gws auth token to get a fresh access token (sync subprocess)."""
        binary_path = shutil.which(self.binary_name)
        if binary_path is None:
            raise CredentialResolutionError(
                "external-cli",
                backend_key,
                f"{self.binary_name}: binary not found on PATH",
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
            data = json.loads(proc.stdout)
            access_token = data.get("access_token", "").strip()
        except (json.JSONDecodeError, AttributeError) as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, f"gws auth token: parse error: {exc}"
            ) from exc

        if not access_token:
            raise CredentialResolutionError(
                "external-cli", backend_key, "gws auth token: empty access_token"
            )

        return ResolvedCredential(kind="bearer_token", access_token=access_token)
