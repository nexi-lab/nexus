"""SubprocessAdapter — base class for external CLIs that require shell-out.

Subclasses declare a binary name, status args, and a parse_output method.
The base class handles: PATH detection, asyncio.create_subprocess_exec,
timeout (default 5s), stderr capture, retry with exponential backoff,
and error classification.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from abc import abstractmethod

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)

logger = logging.getLogger(__name__)


class SubprocessAdapter(ExternalCliSyncAdapter):
    """Base class for subprocess-based sync adapters.

    Subclasses set binary_name, implement get_status_args() and parse_output().
    """

    binary_name: str
    sync_ttl_seconds: float = 300.0  # subprocess = expensive
    _subprocess_timeout: float = 5.0
    _max_retries: int = 1
    _backoff_base: float = 1.0  # seconds; doubles each retry

    @abstractmethod
    def get_status_args(self) -> tuple[str, ...]:
        """Return the CLI arguments to get account/status info."""
        ...

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str) -> list[SyncedProfile]:
        """Parse CLI output into discovered profiles."""
        ...

    async def detect(self) -> bool:
        """Return True if the binary is found on PATH."""
        return shutil.which(self.binary_name) is not None

    async def sync(self) -> SyncResult:
        """Run the CLI command, parse output, retry on transient failure."""
        binary_path = shutil.which(self.binary_name)
        if binary_path is None:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: binary not found on PATH",
            )

        last_error: str | None = None
        for attempt in range(1 + self._max_retries):
            if attempt > 0:
                delay = self._backoff_base * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

            try:
                result = await self._run_once(binary_path)
                if result.error is None:
                    return result
                last_error = result.error
            except Exception as exc:
                last_error = str(exc)

        return SyncResult(
            adapter_name=self.adapter_name,
            error=last_error,
        )

    async def _run_once(self, binary_path: str) -> SyncResult:
        """Single subprocess execution with timeout."""
        args = self.get_status_args()
        try:
            proc = await asyncio.create_subprocess_exec(
                binary_path,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._subprocess_timeout,
            )
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: timeout after {self._subprocess_timeout}s",
            )
        except FileNotFoundError:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: binary not found",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            error_detail = stderr.strip() or f"exit code {proc.returncode}"
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: {error_detail}",
            )

        try:
            profiles = self.parse_output(stdout, stderr)
        except Exception as exc:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: parse error: {exc}",
            )

        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=profiles,
        )
