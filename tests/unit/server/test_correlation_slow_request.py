"""CorrelationMiddleware slow-request warning (#4777).

A 300 s ``200 OK`` used to log at ``info`` exactly like a 20 ms one; slow
requests now log at ``warning`` with ``slow_request=True``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.server.middleware import correlation as mod


def _scope() -> dict[str, Any]:
    return {"type": "http", "method": "POST", "path": "/api/v2/files/write", "headers": []}


def _app_factory(delay: float, status: int = 200) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        if delay:
            await asyncio.sleep(delay)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


async def _receive() -> dict[str, Any]:
    return {"type": "http.request"}


async def _send(_: Any) -> None:
    return None


def test_threshold_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mod.SLOW_REQUEST_MS_ENV, raising=False)
    assert mod.slow_request_threshold_ms() == mod.DEFAULT_SLOW_REQUEST_MS
    monkeypatch.setenv(mod.SLOW_REQUEST_MS_ENV, "2500")
    assert mod.slow_request_threshold_ms() == 2500.0
    monkeypatch.setenv(mod.SLOW_REQUEST_MS_ENV, "garbage")
    assert mod.slow_request_threshold_ms() == mod.DEFAULT_SLOW_REQUEST_MS
    monkeypatch.setenv(mod.SLOW_REQUEST_MS_ENV, "-1")
    assert mod.slow_request_threshold_ms() == 0.0


def test_slow_request_logs_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(mod, "_log", log)
    middleware = mod.CorrelationMiddleware(_app_factory(delay=0.02), slow_request_ms=5.0)

    asyncio.run(middleware(_scope(), _receive, _send))

    log.warning.assert_called_once()
    _, kwargs = log.warning.call_args
    assert kwargs["status_code"] == 200
    assert kwargs["slow_request"] is True
    assert kwargs["slow_threshold_ms"] == 5.0
    assert kwargs["duration_ms"] >= 5.0
    log.info.assert_not_called()


def test_fast_request_logs_info(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(mod, "_log", log)
    middleware = mod.CorrelationMiddleware(_app_factory(delay=0), slow_request_ms=10_000.0)

    asyncio.run(middleware(_scope(), _receive, _send))

    log.info.assert_called_once()
    _, kwargs = log.info.call_args
    assert "slow_request" not in kwargs
    log.warning.assert_not_called()


def test_zero_threshold_disables_slow_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(mod, "_log", log)
    middleware = mod.CorrelationMiddleware(_app_factory(delay=0.01), slow_request_ms=0.0)

    asyncio.run(middleware(_scope(), _receive, _send))

    log.info.assert_called_once()
    log.warning.assert_not_called()


def test_server_error_still_warns_without_slow_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(mod, "_log", log)
    middleware = mod.CorrelationMiddleware(
        _app_factory(delay=0, status=500), slow_request_ms=10_000.0
    )

    asyncio.run(middleware(_scope(), _receive, _send))

    log.warning.assert_called_once()
    _, kwargs = log.warning.call_args
    assert kwargs["status_code"] == 500
    assert "slow_request" not in kwargs
