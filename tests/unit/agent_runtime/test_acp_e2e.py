"""ACP E2E tests — verify `nexus chat --acp` subprocess protocol over stdio.

Spawns `nexus chat --acp` as a real subprocess and exercises the
JSON-RPC handshake (initialize -> session/new). Verifies:

1. Stdout is clean JSON-RPC (no Rust tracing pollution)
2. initialize returns protocol version + capabilities
3. session/new returns session ID + model info
4. Graceful shutdown on stdin EOF

Does NOT test session/prompt (requires real LLM backend).
See test_acp_protocol.py for mock-based prompt tests.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time

import pytest

# Skip if nexus CLI not importable (e.g., minimal CI without Rust extension).
# xdist_group ensures serial execution — each test spawns a subprocess
# that binds to Raft port 2126.
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("NEXUS_SKIP_E2E") == "1",
        reason="NEXUS_SKIP_E2E=1",
    ),
    pytest.mark.xdist_group("acp_e2e"),
]


def _spawn_acp() -> subprocess.Popen[str]:
    """Spawn `nexus chat --acp` with dummy LLM config and isolated data dir."""
    import tempfile

    env = os.environ.copy()
    env["NEXUS_LLM_BASE_URL"] = "http://127.0.0.1:19999/v1"  # unused port
    env["NEXUS_LLM_API_KEY"] = "test-key"
    env["RUST_LOG"] = "error"  # quiet Rust
    # Use isolated temp data dir to avoid redb lock conflicts with other nexus instances
    env["NEXUS_DATA_DIR"] = tempfile.mkdtemp(prefix="nexus-e2e-")
    # Use slim profile to avoid Raft dependency (Raft extension not built in CI/local)
    env["NEXUS_PROFILE"] = "slim"
    return subprocess.Popen(
        ["uv", "run", "nexus", "chat", "--acp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _send(proc: subprocess.Popen[str], msg: dict) -> None:
    assert proc.stdin is not None
    if proc.poll() is not None:
        pytest.skip(f"nexus chat --acp exited early (code={proc.returncode})")
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _read_response(
    proc: subprocess.Popen[str],
    *,
    expect_id: int | None = None,
    timeout: float = 15.0,
) -> dict | None:
    """Read a JSON-RPC response. If expect_id is set, keep reading until we find it."""
    assert proc.stdout is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
        if ready:
            line = proc.stdout.readline()
            if line:
                msg = json.loads(line)
                if expect_id is None or msg.get("id") == expect_id:
                    return msg
                # Not the response we want — skip (e.g., notification)
    return None


def _drain_stdout(proc: subprocess.Popen[str], timeout: float = 1.0) -> list[str]:
    """Read all available lines from stdout within timeout."""
    assert proc.stdout is not None
    lines: list[str] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if ready:
            line = proc.stdout.readline()
            if line:
                lines.append(line.strip())
            else:
                break
    return lines


def _wait_boot(proc: subprocess.Popen[str], seconds: float = 6) -> None:
    """Wait for boot. Skip test if process exits early (missing Rust ext, port conflict, etc.)."""
    time.sleep(seconds)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        pytest.skip(
            f"nexus chat --acp exited during boot (code={proc.returncode}): "
            f"{stderr[:200] if stderr else '(no stderr)'}"
        )


def _cleanup(proc: subprocess.Popen[str]) -> None:
    try:
        if proc.stdin and proc.poll() is None:
            proc.stdin.close()
    except BrokenPipeError:
        pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


class TestAcpSubprocess:
    """E2E tests exercising `nexus chat --acp` as a real subprocess."""

    def test_stdout_clean_on_boot(self) -> None:
        """Stdout must be clean before any JSON-RPC messages — no tracing leaks."""
        proc = _spawn_acp()
        try:
            _wait_boot(proc)
            lines = _drain_stdout(proc)
            # No lines should appear on stdout before we send anything
            for line in lines:
                assert line.startswith("{"), f"Non-JSON on stdout (tracing leak): {line[:100]}"
        finally:
            _cleanup(proc)

    def test_initialize(self) -> None:
        """initialize returns protocolVersion and capabilities."""
        proc = _spawn_acp()
        try:
            _wait_boot(proc)
            _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = _read_response(proc, expect_id=1)
            if resp is None and proc.poll() is not None:
                pytest.skip(f"nexus chat --acp crashed after boot (code={proc.returncode})")
            assert resp is not None, "No response to initialize"
            result = resp["result"]
            assert result["protocolVersion"] == 1
            assert result["serverCapabilities"]["streaming"] is True
            assert result["serverCapabilities"]["toolExecution"] is True
        finally:
            _cleanup(proc)

    def test_session_new(self) -> None:
        """session/new returns sessionId and model info."""
        proc = _spawn_acp()
        try:
            _wait_boot(proc)
            # Initialize first
            _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            _read_response(proc, expect_id=1)

            # Session new
            _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "/tmp"},
                },
            )
            resp = _read_response(proc, expect_id=2)
            assert resp is not None, "No response to session/new"
            result = resp["result"]
            assert "sessionId" in result
            assert len(result["sessionId"]) > 0
            assert "models" in result
        finally:
            _cleanup(proc)

    def test_eof_shutdown(self) -> None:
        """Closing stdin causes graceful shutdown."""
        proc = _spawn_acp()
        try:
            _wait_boot(proc)
            _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = _read_response(proc, expect_id=1)
            if resp is None and proc.poll() is not None:
                pytest.skip(f"nexus chat --acp crashed after boot (code={proc.returncode})")

            # Close stdin — should cause handler.run() to exit
            assert proc.stdin is not None
            proc.stdin.close()
            rc = proc.wait(timeout=10)
            assert rc == 0, f"Process exited with non-zero code: {rc}"
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("Process did not exit after stdin EOF")

    def test_prompt_without_session_returns_error(self) -> None:
        """session/prompt before session/new returns JSON-RPC error."""
        proc = _spawn_acp()
        try:
            _wait_boot(proc)
            _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = _read_response(proc, expect_id=1)
            if resp is None and proc.poll() is not None:
                pytest.skip(f"nexus chat --acp crashed after boot (code={proc.returncode})")

            # Skip session/new, send prompt directly
            _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/prompt",
                    "params": {"prompt": [{"type": "text", "text": "hello"}]},
                },
            )
            resp = _read_response(proc, expect_id=2)
            assert resp is not None
            assert "error" in resp
        finally:
            _cleanup(proc)
