"""Tests for ``nexus ready`` — sandbox/daemon readiness probe (Issue #4126).

Verifies:
  1. Missing readiness file + short timeout → exit(TEMPFAIL), "timeout" in output.
  2. Malformed readiness file (no colon) → exit(DATA_ERROR).
  3. Happy path: valid file + mocked /health 200 + /api/v2/features 200 →
     exit 0, JSON parses to ready:true, profile/endpoint correct.
  4. Valid file but /health never 200 → exit(TEMPFAIL).

All tests use tiny timeouts and patched httpx (no real network/daemon).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli.exit_codes import ExitCode


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _ready_cmd():
    """Resolve the ``ready`` click command the same way other CLI unit
    tests resolve theirs — import the module attribute directly."""
    from nexus.cli.commands.ready import ready

    return ready


# ---------------------------------------------------------------------------
# 1. Timeout waiting for readiness file
# ---------------------------------------------------------------------------


def test_ready_timeout_no_file_exits_tempfail(runner: CliRunner, tmp_path: Path) -> None:
    """No readiness file appears within the timeout → exit(TEMPFAIL)."""
    missing = tmp_path / "does-not-exist.ready"
    result = runner.invoke(
        _ready_cmd(),
        ["--readiness-file", str(missing), "--timeout", "0.5"],
    )
    assert result.exit_code == ExitCode.TEMPFAIL, result.output
    lower = result.output.lower()
    assert "not ready" in lower or "timeout" in lower


# ---------------------------------------------------------------------------
# 2. Malformed readiness file
# ---------------------------------------------------------------------------


def test_ready_malformed_file_exits_data_error(runner: CliRunner, tmp_path: Path) -> None:
    """Readiness file present but unparseable (no colon) → exit(DATA_ERROR)."""
    f = tmp_path / "nexusd.ready"
    f.write_text("garbage-no-colon\n")
    result = runner.invoke(
        _ready_cmd(),
        ["--readiness-file", str(f), "--timeout", "1"],
    )
    assert result.exit_code == ExitCode.DATA_ERROR, result.output
    assert "malformed" in result.output.lower()


# ---------------------------------------------------------------------------
# Mock HTTP helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stand-in for httpx.Client driven by a routing callable."""

    def __init__(self, route, *args, **kwargs) -> None:
        self._route = route

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def get(self, url: str, *args, **kwargs) -> _FakeResponse:
        return self._route(url)


# ---------------------------------------------------------------------------
# 3. Happy path
# ---------------------------------------------------------------------------


def test_ready_happy_path_exits_success_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid file + healthy daemon + features endpoint → exit 0, JSON ready:true."""
    f = tmp_path / "nexusd.ready"
    f.write_text("127.0.0.1:2026\n")

    def route(url: str) -> _FakeResponse:
        if url.endswith("/health"):
            return _FakeResponse(200, {"status": "healthy"})
        if url.endswith("/api/v2/features"):
            return _FakeResponse(200, {"profile": "sandbox", "enabled_bricks": ["search", "mcp"]})
        raise AssertionError(f"unexpected url {url}")

    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(route, *a, **k))

    result = runner.invoke(
        _ready_cmd(),
        ["--readiness-file", str(f), "--json", "--timeout", "5"],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output

    envelope = json.loads(result.output)
    data = envelope["data"]
    assert data["ready"] is True
    assert data["profile"] == "sandbox"
    assert data["endpoint"] == "127.0.0.1:2026"
    assert data["enabled_bricks"] == ["search", "mcp"]


# ---------------------------------------------------------------------------
# 4. Health endpoint never returns 200
# ---------------------------------------------------------------------------


def test_ready_health_never_200_exits_tempfail(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid file but /health always 503 → exit(TEMPFAIL)."""
    f = tmp_path / "nexusd.ready"
    f.write_text("127.0.0.1:2026\n")

    def route(url: str) -> _FakeResponse:
        return _FakeResponse(503)

    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(route, *a, **k))

    result = runner.invoke(
        _ready_cmd(),
        ["--readiness-file", str(f), "--timeout", "1"],
    )
    assert result.exit_code == ExitCode.TEMPFAIL, result.output
    assert "not ready" in result.output.lower()


# ---------------------------------------------------------------------------
# 5. /health 200 but /api/v2/features fails → still ready (best-effort probe)
# ---------------------------------------------------------------------------


def test_ready_features_fail_still_ready(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Healthy /health but failing features endpoint → exit 0, ready:true
    with profile None and empty enabled_bricks (best-effort probe)."""
    f = tmp_path / "nexusd.ready"
    f.write_text("127.0.0.1:2026\n")

    def route(url: str) -> _FakeResponse:
        if url.endswith("/health"):
            return _FakeResponse(200, {"status": "healthy"})
        if url.endswith("/api/v2/features"):
            raise RuntimeError("features endpoint blew up")
        raise AssertionError(f"unexpected url {url}")

    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(route, *a, **k))

    result = runner.invoke(
        _ready_cmd(),
        ["--readiness-file", str(f), "--json", "--timeout", "5"],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output

    envelope = json.loads(result.output)
    data = envelope["data"]
    assert data["ready"] is True
    assert data["profile"] is None
    assert data["enabled_bricks"] == []


# ---------------------------------------------------------------------------
# 6. --timeout 0 → Click usage error (exit 2)
# ---------------------------------------------------------------------------


def test_ready_timeout_zero_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    """Non-positive --timeout is a Click usage error (exit code 2)."""
    f = tmp_path / "nexusd.ready"
    f.write_text("127.0.0.1:2026\n")
    result = runner.invoke(
        _ready_cmd(),
        ["--readiness-file", str(f), "--timeout", "0"],
    )
    assert result.exit_code == 2, result.output
