"""Tests for SubprocessAdapter base class via synthetic adapter."""

from __future__ import annotations

import os
import sys
import tempfile
import uuid

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import SyncedProfile
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter


def _make_synthetic_subprocess(
    *,
    binary_name: str = sys.executable,
    status_args: tuple[str, ...] = (),
    subprocess_timeout: float = 5.0,
    max_retries: int = 1,
) -> SubprocessAdapter:
    """Create a synthetic SubprocessAdapter for testing."""

    class SyntheticSubprocessAdapter(SubprocessAdapter):
        adapter_name = "synthetic-subprocess"

        def __init__(
            self,
            *,
            binary: str,
            args: tuple[str, ...],
            timeout: float,
            retries: int,
        ) -> None:
            self.binary_name = binary
            self._status_args = args
            self._subprocess_timeout = timeout
            self._max_retries = retries

        def get_status_args(self) -> tuple[str, ...]:
            return self._status_args

        def parse_output(self, stdout: str, stderr: str) -> list[SyncedProfile]:  # noqa: ARG002
            lines = [line for line in stdout.strip().splitlines() if line.strip()]
            return [
                SyncedProfile(
                    provider="test",
                    account_identifier=line.strip(),
                    backend_key=f"synthetic/{line.strip()}",
                    source="synthetic-subprocess",
                )
                for line in lines
            ]

        async def resolve_credential(self, backend_key: str) -> ResolvedCredential:  # noqa: ARG002
            return ResolvedCredential(
                kind="api_key",
                api_key="dummy-key",
            )

    return SyntheticSubprocessAdapter(
        binary=binary_name,
        args=status_args,
        timeout=subprocess_timeout,
        retries=max_retries,
    )


class TestDetect:
    async def test_detect_true_for_python(self) -> None:
        adapter = _make_synthetic_subprocess(binary_name=sys.executable)
        assert await adapter.detect() is True

    async def test_detect_false_for_nonexistent_binary(self) -> None:
        adapter = _make_synthetic_subprocess(binary_name="nonexistent-binary-xyz-123456")
        assert await adapter.detect() is False


class TestSyncParsesStdout:
    async def test_sync_parses_two_profiles(self) -> None:
        adapter = _make_synthetic_subprocess(
            status_args=("-c", "print('account1\\naccount2')"),
        )
        result = await adapter.sync()

        assert result.error is None
        assert len(result.profiles) == 2
        assert result.profiles[0].account_identifier == "account1"
        assert result.profiles[1].account_identifier == "account2"
        assert result.adapter_name == "synthetic-subprocess"


class TestStderrOnNonzeroExit:
    async def test_stderr_captured_on_exit_1(self) -> None:
        adapter = _make_synthetic_subprocess(
            status_args=(
                "-c",
                "import sys; sys.stderr.write('auth failed\\n'); sys.exit(1)",
            ),
            max_retries=0,
        )
        result = await adapter.sync()

        assert result.error is not None
        assert "auth failed" in result.error


class TestTimeout:
    async def test_timeout_error(self) -> None:
        adapter = _make_synthetic_subprocess(
            status_args=("-c", "import time; time.sleep(30)"),
            subprocess_timeout=0.5,
            max_retries=0,
        )
        result = await adapter.sync()

        assert result.error is not None
        assert "timeout" in result.error.lower()


class TestMissingBinary:
    async def test_missing_binary_returns_error(self) -> None:
        adapter = _make_synthetic_subprocess(
            binary_name="nonexistent-binary-xyz-123456",
        )
        result = await adapter.sync()

        assert result.error is not None
        assert "not found" in result.error.lower()


class TestRetryOnTransientFailure:
    async def test_retry_succeeds_on_second_attempt(self) -> None:
        marker = os.path.join(
            tempfile.gettempdir(),
            f"subprocess_adapter_test_{uuid.uuid4().hex}",
        )
        try:
            # Script: first call creates marker and fails, second finds marker
            # and succeeds.
            script = (
                "import os, sys\n"
                f"marker = {marker!r}\n"
                "if not os.path.exists(marker):\n"
                "    with open(marker, 'w') as f:\n"
                "        f.write('done')\n"
                "    sys.stderr.write('transient error\\n')\n"
                "    sys.exit(1)\n"
                "else:\n"
                "    print('recovered-account')\n"
            )
            adapter = _make_synthetic_subprocess(
                status_args=("-c", script),
                max_retries=2,
            )
            # Override backoff to speed up the test
            adapter._backoff_base = 0.01

            result = await adapter.sync()

            assert result.error is None
            assert len(result.profiles) == 1
            assert result.profiles[0].account_identifier == "recovered-account"
        finally:
            if os.path.exists(marker):
                os.remove(marker)
